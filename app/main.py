from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from .db import Base, SessionLocal, engine
from .models import ApprovalTask, BlacklistedCounterparty, HistoryEntry, InflowPlan, LimitRule, User
from .security import sha256_hex, verify_password

APP_SECRET = "treasury-demo-secret-2026-03-02"

STATUSES = [
    "Черновик",
    "На проверке",
    "На согласовании",
    "Утверждено",
    "На доработке",
    "Отклонено",
    "Актуализировано",
    "Отменено",
]

CHANNELS = [
    ("account", "Расчетный счет"),
    ("cash", "Касса"),
]

ROLE_LABELS = {
    "initiator": "Инициатор",
    "treasurer": "Казначей",
    "manager": "Финансовый директор/Руководитель",
    "accountant": "Бухгалтерия",
    "admin": "Администратор",
}

app = FastAPI(title="Казначейство — Планирование поступления ДС (демо)")
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory="static"), name="static")

templates_env = Environment(loader=FileSystemLoader("templates"), autoescape=select_autoescape(["html", "xml"]))


def render(template_name: str, **context) -> HTMLResponse:
    template = templates_env.get_template(template_name)
    return HTMLResponse(template.render(**context))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db=Depends(get_db)) -> Optional[User]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def require_user(user: Optional[User]) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def require_role(user: User, allowed: List[str]) -> None:
    if user.role not in allowed:
        raise HTTPException(status_code=403, detail="Недостаточно прав")


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def pop_flash(request: Request) -> Optional[Dict[str, str]]:
    return request.session.pop("flash", None)


def seed_if_empty(db: Session):
    if db.query(User).count() == 0:
        users = [
            User(username="initiator1", password_sha256=sha256_hex("pass123"), role="initiator", full_name="Инициатор 1"),
            User(username="treasurer1", password_sha256=sha256_hex("pass123"), role="treasurer", full_name="Казначей 1"),
            User(username="manager1", password_sha256=sha256_hex("pass123"), role="manager", full_name="ФД 1"),
            User(
                username="accountant1",
                password_sha256=sha256_hex("pass123"),
                role="accountant",
                full_name="Бухгалтер 1",
            ),
            User(username="admin", password_sha256=sha256_hex("admin123"), role="admin", full_name="Администратор"),
        ]
        db.add_all(users)
        db.commit()

    if db.query(LimitRule).count() == 0:
        db.add_all(
            [
                LimitRule(
                    organization="ООО Ромашка",
                    article="Выручка от продаж",
                    currency="RUB",
                    limit_amount=Decimal("500000.00"),
                    always_requires_approval=False,
                ),
                LimitRule(
                    organization="ООО Ромашка",
                    article="Займы и кредиты",
                    currency="RUB",
                    limit_amount=Decimal("0.00"),
                    always_requires_approval=True,
                ),
            ]
        )
        db.commit()

    if db.query(BlacklistedCounterparty).count() == 0:
        db.add(BlacklistedCounterparty(counterparty="ООО РискКонтрагент", reason="Высокий риск/просрочки", active=True))
        db.commit()


def next_number(db: Session) -> str:
    today = dt.datetime.utcnow().strftime("%Y%m%d")
    prefix = f"PPDS-{today}-"
    last = db.query(InflowPlan).filter(InflowPlan.number.like(prefix + "%")).order_by(InflowPlan.number.desc()).first()
    if not last:
        return prefix + "0001"
    tail = last.number.replace(prefix, "")
    try:
        n = int(tail) + 1
    except ValueError:
        n = 1
    return prefix + f"{n:04d}"


def validate_inflow_fields(data: dict) -> List[str]:
    errors = []
    required = ["organization", "counterparty", "contract", "basis", "article", "channel", "planned_date", "amount", "currency"]
    for k in required:
        if k not in data or data[k] in (None, "", "0"):
            errors.append(f"Не заполнено поле: {k}")
    try:
        amount = Decimal(str(data.get("amount", "0")))
        if amount <= 0:
            errors.append("Сумма должна быть больше 0")
    except Exception:
        errors.append("Сумма указана некорректно")
    return errors


def is_counterparty_blacklisted(db: Session, counterparty: str) -> bool:
    row = (
        db.query(BlacklistedCounterparty)
        .filter(BlacklistedCounterparty.counterparty == counterparty, BlacklistedCounterparty.active.is_(True))
        .first()
    )
    return row is not None


def get_limit_rule(db: Session, organization: str, article: str, currency: str) -> Optional[LimitRule]:
    return (
        db.query(LimitRule)
        .filter(LimitRule.organization == organization, LimitRule.article == article, LimitRule.currency == currency)
        .first()
    )


def decide_need_approval(
    db: Session, organization: str, article: str, counterparty: str, amount: Decimal, currency: str
) -> Tuple[bool, str]:
    if is_counterparty_blacklisted(db, counterparty):
        return True, "Контрагент в черном списке/высокий риск"

    rule = get_limit_rule(db, organization, article, currency)
    if rule and rule.always_requires_approval:
        return True, "Тип поступления требует согласования"

    if rule and rule.limit_amount and amount > rule.limit_amount:
        return True, f"Превышение лимита {rule.limit_amount} {currency}"

    return False, "Автоутверждение по правилам"


def add_history(db: Session, inflow: InflowPlan, user: User, field: str, old: str, new: str, reason: str = "") -> None:
    h = HistoryEntry(inflow_id=inflow.id, user_id=user.id, field=field, old_value=str(old), new_value=str(new), reason=reason or "")
    db.add(h)


def set_status(db: Session, inflow: InflowPlan, user: User, new_status: str, reason: str = "") -> None:
    old = inflow.status
    inflow.status = new_status
    inflow.updated_at = dt.datetime.utcnow()
    add_history(db, inflow, user, "status", old, new_status, reason)


def can_edit_inflow(user: User, inflow: InflowPlan) -> bool:
    if user.role in ("admin", "treasurer"):
        return True
    if user.role == "initiator":
        return inflow.initiator_id == user.id and inflow.status in ("Черновик", "На доработке")
    return False


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_if_empty(db)
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/inflows", status_code=HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, db=Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse("/inflows", status_code=HTTP_303_SEE_OTHER)
    return render("login.html", request=request, flash=pop_flash(request), role_labels=ROLE_LABELS)


@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...), db=Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_sha256):
        flash(request, "Неверный логин или пароль", "error")
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)

    request.session["user_id"] = user.id
    flash(request, f"Вы вошли как {user.full_name} ({ROLE_LABELS.get(user.role, user.role)})", "success")
    return RedirectResponse("/inflows", status_code=HTTP_303_SEE_OTHER)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/inflows", response_class=HTMLResponse)
def inflows_list(request: Request, status: Optional[str] = None, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    q = db.query(InflowPlan)

    if user.role == "initiator":
        q = q.filter(InflowPlan.initiator_id == user.id)

    if status:
        q = q.filter(InflowPlan.status == status)

    inflows = q.order_by(InflowPlan.created_at.desc()).all()
    return render(
        "inflows_list.html",
        request=request,
        user=user,
        inflows=inflows,
        statuses=STATUSES,
        selected_status=status or "",
        flash=pop_flash(request),
        role_labels=ROLE_LABELS,
    )


@app.get("/inflows/new", response_class=HTMLResponse)
def inflows_new_get(request: Request, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    require_role(user, ["initiator", "treasurer", "admin"])
    return render(
        "inflow_form.html",
        request=request,
        user=user,
        mode="create",
        inflow=None,
        channels=CHANNELS,
        statuses=STATUSES,
        flash=pop_flash(request),
        role_labels=ROLE_LABELS,
    )


@app.post("/inflows/new")
def inflows_new_post(
    request: Request,
    organization: str = Form(...),
    counterparty: str = Form(...),
    contract: str = Form(...),
    basis: str = Form(...),
    article: str = Form(...),
    channel: str = Form(...),
    planned_date: str = Form(...),
    amount: str = Form(...),
    currency: str = Form(...),
    comment: str = Form(""),
    db=Depends(get_db),
):
    user = require_user(get_current_user(request, db))
    require_role(user, ["initiator", "treasurer", "admin"])

    try:
        planned_date_dt = dt.date.fromisoformat(planned_date)
    except Exception:
        flash(request, "Некорректная дата (используй формат YYYY-MM-DD)", "error")
        return RedirectResponse("/inflows/new", status_code=HTTP_303_SEE_OTHER)

    try:
        amount_dec = Decimal(amount)
    except Exception:
        flash(request, "Некорректная сумма", "error")
        return RedirectResponse("/inflows/new", status_code=HTTP_303_SEE_OTHER)

    number = next_number(db)
    inflow = InflowPlan(
        number=number,
        organization=organization.strip(),
        counterparty=counterparty.strip(),
        contract=contract.strip(),
        basis=basis.strip(),
        article=article.strip(),
        channel=channel,
        planned_date=planned_date_dt,
        amount=amount_dec,
        currency=currency.strip().upper(),
        status="Черновик",
        comment=comment.strip(),
        initiator_id=user.id,
        change_reason="",
    )
    db.add(inflow)
    db.flush()
    add_history(db, inflow, user, "create", "", f"Создан документ {inflow.number}", "")
    db.commit()

    flash(request, f"Создано планируемое поступление {inflow.number}", "success")
    return RedirectResponse(f"/inflows/{inflow.id}", status_code=HTTP_303_SEE_OTHER)


@app.get("/inflows/{inflow_id}", response_class=HTMLResponse)
def inflow_detail(request: Request, inflow_id: int, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    inflow = db.get(InflowPlan, inflow_id)
    if not inflow:
        raise HTTPException(status_code=404, detail="Не найдено")

    if user.role == "initiator" and inflow.initiator_id != user.id:
        raise HTTPException(status_code=403, detail="Нет доступа")

    last_task = (
        db.query(ApprovalTask).filter(ApprovalTask.inflow_id == inflow.id).order_by(ApprovalTask.created_at.desc()).first()
    )

    return render(
        "inflow_detail.html",
        request=request,
        user=user,
        inflow=inflow,
        can_edit=can_edit_inflow(user, inflow),
        channels=dict(CHANNELS),
        last_task=last_task,
        flash=pop_flash(request),
        role_labels=ROLE_LABELS,
    )


@app.get("/inflows/{inflow_id}/edit", response_class=HTMLResponse)
def inflow_edit_get(request: Request, inflow_id: int, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    inflow = db.get(InflowPlan, inflow_id)
    if not inflow:
        raise HTTPException(status_code=404, detail="Не найдено")
    if user.role == "initiator" and inflow.initiator_id != user.id:
        raise HTTPException(status_code=403, detail="Нет доступа")
    if not can_edit_inflow(user, inflow):
        flash(request, "Редактирование запрещено для текущего статуса/роли", "error")
        return RedirectResponse(f"/inflows/{inflow.id}", status_code=HTTP_303_SEE_OTHER)

    return render(
        "inflow_form.html",
        request=request,
        user=user,
        mode="edit",
        inflow=inflow,
        channels=CHANNELS,
        statuses=STATUSES,
        flash=pop_flash(request),
        role_labels=ROLE_LABELS,
    )


@app.post("/inflows/{inflow_id}/edit")
def inflow_edit_post(
    request: Request,
    inflow_id: int,
    organization: str = Form(...),
    counterparty: str = Form(...),
    contract: str = Form(...),
    basis: str = Form(...),
    article: str = Form(...),
    channel: str = Form(...),
    planned_date: str = Form(...),
    amount: str = Form(...),
    currency: str = Form(...),
    comment: str = Form(""),
    change_reason: str = Form(""),
    db=Depends(get_db),
):
    user = require_user(get_current_user(request, db))
    inflow = db.get(InflowPlan, inflow_id)
    if not inflow:
        raise HTTPException(status_code=404, detail="Не найдено")
    if user.role == "initiator" and inflow.initiator_id != user.id:
        raise HTTPException(status_code=403, detail="Нет доступа")
    if not can_edit_inflow(user, inflow):
        flash(request, "Редактирование запрещено для текущего статуса/роли", "error")
        return RedirectResponse(f"/inflows/{inflow.id}", status_code=HTTP_303_SEE_OTHER)

    try:
        planned_date_dt = dt.date.fromisoformat(planned_date)
    except Exception:
        flash(request, "Некорректная дата (используй формат YYYY-MM-DD)", "error")
        return RedirectResponse(f"/inflows/{inflow.id}/edit", status_code=HTTP_303_SEE_OTHER)

    try:
        amount_dec = Decimal(amount)
    except Exception:
        flash(request, "Некорректная сумма", "error")
        return RedirectResponse(f"/inflows/{inflow.id}/edit", status_code=HTTP_303_SEE_OTHER)

    def upd(field_name: str, new_value):
        old_value = getattr(inflow, field_name)
        if str(old_value) != str(new_value):
            add_history(db, inflow, user, field_name, str(old_value), str(new_value), change_reason.strip())
            setattr(inflow, field_name, new_value)

    upd("organization", organization.strip())
    upd("counterparty", counterparty.strip())
    upd("contract", contract.strip())
    upd("basis", basis.strip())
    upd("article", article.strip())
    upd("channel", channel)
    upd("planned_date", planned_date_dt)
    upd("amount", amount_dec)
    upd("currency", currency.strip().upper())
    upd("comment", comment.strip())

    if change_reason.strip():
        inflow.change_reason = change_reason.strip()

    inflow.updated_at = dt.datetime.utcnow()

    if inflow.status in ("Утверждено", "Актуализировано") and user.role in ("treasurer", "admin"):
        set_status(db, inflow, user, "Актуализировано", change_reason.strip())

    db.commit()
    flash(request, f"Изменения сохранены для {inflow.number}", "success")
    return RedirectResponse(f"/inflows/{inflow.id}", status_code=HTTP_303_SEE_OTHER)


@app.post("/inflows/{inflow_id}/submit")
def inflow_submit(request: Request, inflow_id: int, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    inflow = db.get(InflowPlan, inflow_id)
    if not inflow:
        raise HTTPException(status_code=404, detail="Не найдено")
    if user.role == "initiator" and inflow.initiator_id != user.id:
        raise HTTPException(status_code=403, detail="Нет доступа")
    if user.role not in ("initiator", "treasurer", "admin"):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    if inflow.status not in ("Черновик", "На доработке"):
        flash(request, f"Нельзя отправить на обработку из статуса: {inflow.status}", "error")
        return RedirectResponse(f"/inflows/{inflow.id}", status_code=HTTP_303_SEE_OTHER)

    data = {
        "organization": inflow.organization,
        "counterparty": inflow.counterparty,
        "contract": inflow.contract,
        "basis": inflow.basis,
        "article": inflow.article,
        "channel": inflow.channel,
        "planned_date": inflow.planned_date,
        "amount": inflow.amount,
        "currency": inflow.currency,
    }
    errors = validate_inflow_fields(data)
    set_status(db, inflow, user, "На проверке", "")

    if errors:
        set_status(db, inflow, user, "На доработке", "; ".join(errors))
        db.commit()
        flash(request, "Найдены ошибки: " + "; ".join(errors), "error")
        return RedirectResponse(f"/inflows/{inflow.id}", status_code=HTTP_303_SEE_OTHER)

    need_approval, reason = decide_need_approval(
        db,
        inflow.organization,
        inflow.article,
        inflow.counterparty,
        Decimal(inflow.amount),
        inflow.currency,
    )

    if need_approval:
        set_status(db, inflow, user, "На согласовании", reason)
        manager = db.query(User).filter(User.role == "manager").order_by(User.id.asc()).first()
        sla_due = dt.datetime.utcnow() + dt.timedelta(days=1)
        task = ApprovalTask(
            inflow_id=inflow.id,
            status="Ожидает",
            sla_due=sla_due,
            approver_id=manager.id if manager else None,
            decision_comment="",
            decided_at=None,
        )
        db.add(task)
        add_history(db, inflow, user, "approval_task", "", "Создана задача согласования", reason)
        db.commit()
        flash(request, "Отправлено на согласование: " + reason, "success")
        return RedirectResponse(f"/inflows/{inflow.id}", status_code=HTTP_303_SEE_OTHER)

    set_status(db, inflow, user, "Утверждено", reason)
    add_history(db, inflow, user, "publish", "", "Публикация в платежный календарь (логическая)", reason)
    db.commit()
    flash(request, "Автоутверждение: " + reason, "success")
    return RedirectResponse(f"/inflows/{inflow.id}", status_code=HTTP_303_SEE_OTHER)


@app.post("/inflows/{inflow_id}/cancel")
def inflow_cancel(request: Request, inflow_id: int, reason: str = Form(""), db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    inflow = db.get(InflowPlan, inflow_id)
    if not inflow:
        raise HTTPException(status_code=404, detail="Не найдено")
    if user.role not in ("treasurer", "admin"):
        raise HTTPException(status_code=403, detail="Только казначей/админ может отменять")
    if inflow.status in ("Отменено", "Отклонено"):
        flash(request, "Документ уже закрыт", "error")
        return RedirectResponse(f"/inflows/{inflow.id}", status_code=HTTP_303_SEE_OTHER)

    set_status(db, inflow, user, "Отменено", reason.strip())
    db.commit()
    flash(request, "Документ отменён", "success")
    return RedirectResponse(f"/inflows/{inflow.id}", status_code=HTTP_303_SEE_OTHER)


@app.get("/inflows/{inflow_id}/history", response_class=HTMLResponse)
def inflow_history(request: Request, inflow_id: int, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    inflow = db.get(InflowPlan, inflow_id)
    if not inflow:
        raise HTTPException(status_code=404, detail="Не найдено")
    if user.role == "initiator" and inflow.initiator_id != user.id:
        raise HTTPException(status_code=403, detail="Нет доступа")

    history = db.query(HistoryEntry).filter(HistoryEntry.inflow_id == inflow.id).order_by(HistoryEntry.changed_at.desc()).all()
    return render(
        "history.html",
        request=request,
        user=user,
        inflow=inflow,
        history=history,
        flash=pop_flash(request),
        role_labels=ROLE_LABELS,
    )


@app.get("/approvals", response_class=HTMLResponse)
def approvals_list(request: Request, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    require_role(user, ["manager", "admin"])
    tasks = db.query(ApprovalTask).filter(ApprovalTask.status == "Ожидает").order_by(ApprovalTask.created_at.asc()).all()
    now = dt.datetime.utcnow()
    return render(
        "approvals.html",
        request=request,
        user=user,
        tasks=tasks,
        now=now,
        flash=pop_flash(request),
        role_labels=ROLE_LABELS,
    )


def decide_task(db: Session, request: Request, task_id: int, action: str, comment: str, user: User):
    task = db.get(ApprovalTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    inflow = task.inflow
    if task.status != "Ожидает":
        flash(request, "Задача уже закрыта", "error")
        return RedirectResponse("/approvals", status_code=HTTP_303_SEE_OTHER)

    task.approver_id = user.id
    task.decided_at = dt.datetime.utcnow()
    task.decision_comment = comment.strip()

    if action == "approve":
        task.status = "Утверждено"
        set_status(db, inflow, user, "Утверждено", comment.strip())
        add_history(db, inflow, user, "approval", "Ожидает", "Утверждено", comment.strip())
        add_history(db, inflow, user, "publish", "", "Публикация в платежный календарь (логическая)", "")
        flash(request, "Утверждено", "success")
    elif action == "return":
        task.status = "Возврат"
        set_status(db, inflow, user, "На доработке", comment.strip())
        add_history(db, inflow, user, "approval", "Ожидает", "Возврат", comment.strip())
        flash(request, "Возвращено на доработку", "success")
    elif action == "reject":
        task.status = "Отклонено"
        set_status(db, inflow, user, "Отклонено", comment.strip())
        add_history(db, inflow, user, "approval", "Ожидает", "Отклонено", comment.strip())
        flash(request, "Отклонено", "success")
    else:
        raise HTTPException(status_code=400, detail="Некорректное действие")

    db.commit()
    return RedirectResponse("/approvals", status_code=HTTP_303_SEE_OTHER)


@app.post("/approvals/{task_id}/approve")
def approval_approve(request: Request, task_id: int, comment: str = Form(""), db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    require_role(user, ["manager", "admin"])
    return decide_task(db, request, task_id, "approve", comment, user)


@app.post("/approvals/{task_id}/return")
def approval_return(request: Request, task_id: int, comment: str = Form(""), db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    require_role(user, ["manager", "admin"])
    return decide_task(db, request, task_id, "return", comment, user)


@app.post("/approvals/{task_id}/reject")
def approval_reject(request: Request, task_id: int, comment: str = Form(""), db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    require_role(user, ["manager", "admin"])
    return decide_task(db, request, task_id, "reject", comment, user)


@app.get("/settings/limits", response_class=HTMLResponse)
def settings_limits(request: Request, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    require_role(user, ["admin", "treasurer"])
    rules = db.query(LimitRule).order_by(LimitRule.organization.asc(), LimitRule.article.asc()).all()
    return render(
        "limits.html",
        request=request,
        user=user,
        rules=rules,
        flash=pop_flash(request),
        role_labels=ROLE_LABELS,
    )


@app.post("/settings/limits/new")
def limits_new(
    request: Request,
    organization: str = Form(...),
    article: str = Form(...),
    currency: str = Form("RUB"),
    limit_amount: str = Form("0"),
    always_requires_approval: Optional[str] = Form(None),
    db=Depends(get_db),
):
    user = require_user(get_current_user(request, db))
    require_role(user, ["admin", "treasurer"])
    try:
        lim = Decimal(limit_amount)
    except Exception:
        flash(request, "Некорректный лимит", "error")
        return RedirectResponse("/settings/limits", status_code=HTTP_303_SEE_OTHER)

    rule = LimitRule(
        organization=organization.strip(),
        article=article.strip(),
        currency=currency.strip().upper(),
        limit_amount=lim,
        always_requires_approval=(always_requires_approval == "on"),
    )
    db.add(rule)
    try:
        db.commit()
    except Exception:
        db.rollback()
        flash(request, "Не удалось сохранить (возможно, правило уже существует)", "error")
        return RedirectResponse("/settings/limits", status_code=HTTP_303_SEE_OTHER)

    flash(request, "Правило лимита добавлено", "success")
    return RedirectResponse("/settings/limits", status_code=HTTP_303_SEE_OTHER)


@app.post("/settings/limits/{rule_id}/delete")
def limits_delete(request: Request, rule_id: int, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    require_role(user, ["admin", "treasurer"])
    rule = db.get(LimitRule, rule_id)
    if rule:
        db.delete(rule)
        db.commit()
        flash(request, "Правило удалено", "success")
    return RedirectResponse("/settings/limits", status_code=HTTP_303_SEE_OTHER)


@app.get("/settings/blacklist", response_class=HTMLResponse)
def settings_blacklist(request: Request, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    require_role(user, ["admin", "treasurer"])
    items = db.query(BlacklistedCounterparty).order_by(BlacklistedCounterparty.created_at.desc()).all()
    return render(
        "blacklist.html",
        request=request,
        user=user,
        items=items,
        flash=pop_flash(request),
        role_labels=ROLE_LABELS,
    )


@app.post("/settings/blacklist/new")
def blacklist_new(
    request: Request,
    counterparty: str = Form(...),
    reason: str = Form(""),
    active: Optional[str] = Form(None),
    db=Depends(get_db),
):
    user = require_user(get_current_user(request, db))
    require_role(user, ["admin", "treasurer"])

    item = BlacklistedCounterparty(counterparty=counterparty.strip(), reason=reason.strip(), active=(active == "on"))
    db.add(item)
    try:
        db.commit()
    except Exception:
        db.rollback()
        flash(request, "Не удалось добавить (возможно, уже есть)", "error")
        return RedirectResponse("/settings/blacklist", status_code=HTTP_303_SEE_OTHER)

    flash(request, "Добавлено в черный список", "success")
    return RedirectResponse("/settings/blacklist", status_code=HTTP_303_SEE_OTHER)


@app.post("/settings/blacklist/{item_id}/toggle")
def blacklist_toggle(request: Request, item_id: int, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    require_role(user, ["admin", "treasurer"])
    item = db.get(BlacklistedCounterparty, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Не найдено")
    item.active = not item.active
    db.commit()
    flash(request, "Статус записи изменён", "success")
    return RedirectResponse("/settings/blacklist", status_code=HTTP_303_SEE_OTHER)


@app.get("/reports/pending", response_class=HTMLResponse)
def report_pending(request: Request, db=Depends(get_db)):
    user = require_user(get_current_user(request, db))
    require_role(user, ["treasurer", "manager", "admin"])
    inflows = db.query(InflowPlan).filter(InflowPlan.status == "На согласовании").order_by(InflowPlan.created_at.desc()).all()
    return render(
        "report_pending.html",
        request=request,
        user=user,
        inflows=inflows,
        channels=dict(CHANNELS),
        flash=pop_flash(request),
        role_labels=ROLE_LABELS,
    )
