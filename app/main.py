from fastapi import FastAPI, Form, HTTPException
from datetime import datetime, date, timedelta
from pathlib import Path
import json
from enum import IntEnum, Enum
from pydantic import BaseModel, Field
from typing import Literal, Optional

from sqlmodel import SQLModel, Field as SQLField, Session, create_engine, select
from sqlalchemy import UniqueConstraint

# ==========
# Types / Models (API input)
# ==========

class Condition(IntEnum):
    bad = 1
    meh = 2
    ok = 3
    good = 4
    great = 5

class Exercise(str, Enum):
    bench_press = "Bench Press"
    squat = "Squat"
    deadlift = "Deadlift"

class WeightIn(BaseModel):
    weight: float = Field(..., ge=0, le=300)
    sleep_hours: float = Field(..., ge=0, le=24)
    condition: Condition
    log_date: date | None = None

class WorkoutIn(BaseModel):
    exercise: Literal["Bench Press", "Squat", "Deadlift"]
    weight: float = Field(..., ge=0, le=500)
    reps: int = Field(..., ge=1, le=50)
    sets: int = Field(1, ge=1, le=20)
    log_date: date | None = None

# Lv2: day summary response
class DaySummary(BaseModel):
    log_date: date
    weight: float | None = None
    sleep_hours: float | None = None
    condition: int | None = None
    workouts: list[dict] = []

# ==========
# DB
# ==========

DB_FILE = "condition_compass.db"
engine = create_engine(f"sqlite:///{DB_FILE}", echo=False)

class WeightLog(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("log_date", name="uq_weight_log_date"),)

    id: Optional[int] = SQLField(default=None, primary_key=True)
    weight: float
    sleep_hours: float
    condition: int
    log_date: date = SQLField(index=True)
    saved_at: str

class WorkoutLog(SQLModel, table=True):
    id: Optional[int] = SQLField(default=None, primary_key=True)
    exercise: str = SQLField(index=True)
    weight: float
    reps: int
    sets: int
    log_date: date = SQLField(index=True)
    saved_at: str

def init_db():
    SQLModel.metadata.create_all(engine)

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

# ==========
# Legacy JSON (migration only)
# ==========

DATA_FILE = Path("weights.json")
WORKOUT_FILE = Path("workouts.json")

def load_json(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []

def _safe_iso_date(it: dict) -> date | None:
    """
    JSONレコードから日付を取り出す（壊れてても落ちない）
    優先順: log_date -> date -> saved_at(日付部分)
    """
    v = it.get("log_date") or it.get("date")
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass

    sa = it.get("saved_at")
    if isinstance(sa, str) and len(sa) >= 10:
        try:
            return date.fromisoformat(sa[:10])
        except ValueError:
            pass

    return None

def migrate_json_to_db_once():
    """
    既にDBに何か入ってたら何もしない（重複移行を防ぐ）
    JSONに欠損キーがあっても落ちない
    """
    with Session(engine) as s:
        has_any = (
            s.exec(select(WeightLog.id)).first() is not None
            or s.exec(select(WorkoutLog.id)).first() is not None
        )
        if has_any:
            return

        # --- weights.json -> WeightLog (upsertルール適用) ---
        skipped_w = 0
        for it in load_json(DATA_FILE):
            d = _safe_iso_date(it)
            if not d:
                skipped_w += 1
                continue

            if "weight" not in it or "sleep_hours" not in it or "condition" not in it:
                skipped_w += 1
                continue

            existing = s.exec(select(WeightLog).where(WeightLog.log_date == d)).first()
            saved_at = it.get("saved_at", now_iso())

            if existing:
                existing.weight = float(it["weight"])
                existing.sleep_hours = float(it["sleep_hours"])
                existing.condition = int(it["condition"])
                existing.saved_at = saved_at
            else:
                s.add(
                    WeightLog(
                        weight=float(it["weight"]),
                        sleep_hours=float(it["sleep_hours"]),
                        condition=int(it["condition"]),
                        log_date=d,
                        saved_at=saved_at,
                    )
                )

        # --- workouts.json -> WorkoutLog ---
        skipped_wo = 0
        for it in load_json(WORKOUT_FILE):
            d = _safe_iso_date(it)
            if not d:
                skipped_wo += 1
                continue

            if "exercise" not in it or "weight" not in it or "reps" not in it:
                skipped_wo += 1
                continue

            s.add(
                WorkoutLog(
                    exercise=str(it["exercise"]),
                    weight=float(it["weight"]),
                    reps=int(it["reps"]),
                    sets=int(it.get("sets", 1)),
                    log_date=d,
                    saved_at=it.get("saved_at", now_iso()),
                )
            )

        s.commit()
        print(f"[migrate] skipped weights={skipped_w}, skipped workouts={skipped_wo}")

# ==========
# App
# ==========

app = FastAPI()

@app.on_event("startup")
def on_startup():
    init_db()
    migrate_json_to_db_once()

# ==========
# UPSERT helper (weights: 1/day)
# ==========

def upsert_weight(d: date, payload: WeightIn) -> WeightLog:
    with Session(engine) as s:
        existing = s.exec(select(WeightLog).where(WeightLog.log_date == d)).first()
        if existing:
            existing.weight = payload.weight
            existing.sleep_hours = payload.sleep_hours
            existing.condition = int(payload.condition)
            existing.saved_at = now_iso()
            s.add(existing)
            s.commit()
            s.refresh(existing)
            return existing

        entry = WeightLog(
            weight=payload.weight,
            sleep_hours=payload.sleep_hours,
            condition=int(payload.condition),
            log_date=d,
            saved_at=now_iso(),
        )
        s.add(entry)
        s.commit()
        s.refresh(entry)
        return entry

# ==========
# Lv2 helpers
# ==========

def _workout_to_dict(w: WorkoutLog) -> dict:
    return {
        "id": w.id,
        "exercise": w.exercise,
        "weight": w.weight,
        "reps": w.reps,
        "sets": w.sets,
        "log_date": w.log_date,
        "saved_at": w.saved_at,
    }

# ==========
# Routes
# ==========

@app.get("/")
def read_root():
    return {"message": "Condition Compass is running"}

# --- Meta ---

@app.get("/meta/exercises")
def meta_exercises():
    return {"items": [e.value for e in Exercise]}

# --- Weights (UPSERT 1/day) ---

@app.post("/weights")
def add_weight(payload: WeightIn):
    d = payload.log_date or date.today()
    entry = upsert_weight(d, payload)
    return {"ok": True, "item": entry, "mode": "upsert"}

@app.put("/weights/{log_date}")
def put_weight(log_date: date, payload: WeightIn):
    entry = upsert_weight(log_date, payload)
    return {"ok": True, "item": entry, "mode": "upsert"}

@app.get("/weights")
def list_weights(from_date: date | None = None, to_date: date | None = None):
    stmt = select(WeightLog)
    if from_date:
        stmt = stmt.where(WeightLog.log_date >= from_date)
    if to_date:
        stmt = stmt.where(WeightLog.log_date <= to_date)
    stmt = stmt.order_by(WeightLog.log_date.asc())

    with Session(engine) as s:
        items = list(s.exec(stmt))
    return {"count": len(items), "items": items}

@app.get("/weights/latest")
def latest_weight():
    stmt = select(WeightLog).order_by(WeightLog.log_date.desc(), WeightLog.id.desc()).limit(1)
    with Session(engine) as s:
        item = s.exec(stmt).first()
    return {"item": item}

@app.get("/weights/recent")
def weights_recent(days: int = 14):
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be 1..365")
    start = date.today() - timedelta(days=days - 1)
    stmt = (
        select(WeightLog)
        .where(WeightLog.log_date >= start)
        .order_by(WeightLog.log_date.asc())
    )
    with Session(engine) as s:
        items = list(s.exec(stmt))
    return {
        "days": days,
        "from_date": start,
        "to_date": date.today(),
        "count": len(items),
        "items": items,
    }

# --- Workouts (multiple/day OK) ---

@app.post("/workouts")
def add_workout(payload: WorkoutIn):
    d = payload.log_date or date.today()
    entry = WorkoutLog(
        exercise=payload.exercise,
        weight=payload.weight,
        reps=payload.reps,
        sets=payload.sets,
        log_date=d,
        saved_at=now_iso(),
    )
    with Session(engine) as s:
        s.add(entry)
        s.commit()
        s.refresh(entry)
    return {"ok": True, "added": entry}

@app.post("/workouts/form")
def add_workout_form(
    exercise: Literal["Bench Press", "Squat", "Deadlift"] = Form(...),
    weight: float = Form(...),
    reps: int = Form(...),
    sets: int = Form(1),
    log_date: str | None = Form(None),
):
    d = date.fromisoformat(log_date) if log_date else date.today()
    entry = WorkoutLog(
        exercise=exercise,
        weight=weight,
        reps=reps,
        sets=sets,
        log_date=d,
        saved_at=now_iso(),
    )
    with Session(engine) as s:
        s.add(entry)
        s.commit()
        s.refresh(entry)
    return {"ok": True, "added": entry}

@app.get("/workouts")
def list_workouts(exercise: str | None = None, from_date: date | None = None, to_date: date | None = None):
    stmt = select(WorkoutLog)
    if exercise:
        stmt = stmt.where(WorkoutLog.exercise == exercise)
    if from_date:
        stmt = stmt.where(WorkoutLog.log_date >= from_date)
    if to_date:
        stmt = stmt.where(WorkoutLog.log_date <= to_date)
    stmt = stmt.order_by(WorkoutLog.log_date.asc(), WorkoutLog.id.asc())

    with Session(engine) as s:
        items = list(s.exec(stmt))
    return {"count": len(items), "items": items}

@app.get("/workouts/latest")
def latest_workout():
    stmt = select(WorkoutLog).order_by(WorkoutLog.log_date.desc(), WorkoutLog.id.desc()).limit(1)
    with Session(engine) as s:
        item = s.exec(stmt).first()
    return {"item": item}

@app.get("/workouts/recent")
def workouts_recent(days: int = 14, exercise: str | None = None):
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be 1..365")
    start = date.today() - timedelta(days=days - 1)

    stmt = select(WorkoutLog).where(WorkoutLog.log_date >= start)
    if exercise:
        stmt = stmt.where(WorkoutLog.exercise == exercise)
    stmt = stmt.order_by(WorkoutLog.log_date.asc(), WorkoutLog.id.asc())

    with Session(engine) as s:
        items = list(s.exec(stmt))
    return {
        "days": days,
        "from_date": start,
        "to_date": date.today(),
        "count": len(items),
        "items": items,
    }

# --- Day summaries (Lv2 main) ---

@app.get("/days/{log_date}")
def day_summary(log_date: date):
    with Session(engine) as s:
        w = s.exec(select(WeightLog).where(WeightLog.log_date == log_date)).first()
        ws = list(
            s.exec(
                select(WorkoutLog)
                .where(WorkoutLog.log_date == log_date)
                .order_by(WorkoutLog.id.asc())
            )
        )

    return DaySummary(
        log_date=log_date,
        weight=w.weight if w else None,
        sleep_hours=w.sleep_hours if w else None,
        condition=w.condition if w else None,
        workouts=[_workout_to_dict(x) for x in ws],
    )

@app.get("/days")
def days_range(from_date: date | None = None, to_date: date | None = None, days: int | None = None):
    if days is not None:
        if days < 1 or days > 365:
            raise HTTPException(status_code=400, detail="days must be 1..365")
        to_date = date.today()
        from_date = to_date - timedelta(days=days - 1)

    if not from_date or not to_date:
        raise HTTPException(status_code=400, detail="Provide (from_date & to_date) OR days")
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="from_date must be <= to_date")

    with Session(engine) as s:
        weights = list(
            s.exec(
                select(WeightLog).where(
                    WeightLog.log_date >= from_date,
                    WeightLog.log_date <= to_date,
                )
            )
        )
        workouts = list(
            s.exec(
                select(WorkoutLog)
                .where(
                    WorkoutLog.log_date >= from_date,
                    WorkoutLog.log_date <= to_date,
                )
                .order_by(WorkoutLog.log_date.asc(), WorkoutLog.id.asc())
            )
        )

    weight_by_date = {x.log_date: x for x in weights}
    workouts_by_date: dict[date, list[WorkoutLog]] = {}
    for w in workouts:
        workouts_by_date.setdefault(w.log_date, []).append(w)

    out: list[DaySummary] = []
    cur = from_date
    while cur <= to_date:
        wl = weight_by_date.get(cur)
        out.append(
            DaySummary(
                log_date=cur,
                weight=wl.weight if wl else None,
                sleep_hours=wl.sleep_hours if wl else None,
                condition=wl.condition if wl else None,
                workouts=[_workout_to_dict(x) for x in workouts_by_date.get(cur, [])],
            )
        )
        cur += timedelta(days=1)

    return {
        "from_date": from_date,
        "to_date": to_date,
        "days": (to_date - from_date).days + 1,
        "items": out,
    }

@app.get("/debug")
def debug():
    return {"db": DB_FILE, "file": __file__}

from fastapi.responses import HTMLResponse

@app.get("/charts", response_class=HTMLResponse)
def charts(days: int = 30):
    # FastAPIが配る “同一オリジン” のページなのでCORS不要
    # /days?days=xx を叩いて体重/睡眠/体調を折れ線で表示する
    return f"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Condition Compass Charts</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; margin: 24px; }}
    .row {{ display:flex; gap:16px; flex-wrap:wrap; align-items:center; }}
    .card {{ border:1px solid #ddd; border-radius:12px; padding:16px; max-width:980px; }}
    canvas {{ max-width: 940px; }}
    input {{ padding:8px 10px; border:1px solid #ccc; border-radius:10px; width:90px; }}
    button {{ padding:8px 12px; border:1px solid #ccc; border-radius:10px; background:#fff; cursor:pointer; }}
  </style>
</head>
<body>
  <h2>Condition Compass（直近グラフ）</h2>

  <div class="row card">
    <div>days:</div>
    <input id="days" type="number" min="1" max="365" value="{days}" />
    <button id="reload">Reload</button>
    <div id="status" style="opacity:.7;"></div>
  </div>

  <div class="card">
    <canvas id="chart"></canvas>
  </div>

  <script>
    let chart;

    function toSeries(items, key) {{
      // nullの日は飛ばさず “欠損” として表示したいので null を入れる
      return items.map(x => x[key] ?? null);
    }}

    async function load(days) {{
      const status = document.getElementById("status");
      status.textContent = "loading...";
      const res = await fetch(`/days?days=${{days}}`);
      const data = await res.json();
      const items = data.items || [];

      const labels = items.map(x => x.log_date);
      const weight = toSeries(items, "weight");
      const sleep  = toSeries(items, "sleep_hours");
      const cond   = toSeries(items, "condition");

      const ctx = document.getElementById("chart").getContext("2d");

      if (chart) chart.destroy();
      chart = new Chart(ctx, {{
        type: "line",
        data: {{
          labels,
          datasets: [
            {{ label: "Weight", data: weight, spanGaps: false, yAxisID: "yWeight" }},
            {{ label: "Sleep (hours)", data: sleep, spanGaps: false, yAxisID: "ySleep" }},
            {{ label: "Condition (1-5)", data: cond, spanGaps: false, yAxisID: "yCond" }},
          ]
        }},
        options: {{
          responsive: true,
          interaction: {{ mode: "index", intersect: false }},
          plugins: {{
            legend: {{ position: "top" }},
            tooltip: {{ enabled: true }}
          }},
          scales: {{
            yWeight: {{ type: "linear", position: "left", title: {{ display: true, text: "kg" }} }},
            ySleep:  {{ type: "linear", position: "right", grid: {{ drawOnChartArea: false }}, title: {{ display: true, text: "hours" }} }},
            yCond:   {{ type: "linear", position: "right", grid: {{ drawOnChartArea: false }}, min: 1, max: 5, ticks: {{ stepSize: 1 }}, title: {{ display: true, text: "1-5" }} }},
          }}
        }}
      }});

      status.textContent = `loaded: ${{items.length}} days`;
    }}

    document.getElementById("reload").addEventListener("click", () => {{
      const d = Number(document.getElementById("days").value || 30);
      load(d);
    }});

    load(Number(document.getElementById("days").value || 30));
  </script>
</body>
</html>
"""
