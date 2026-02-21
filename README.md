# Condition Compass

A personal health & workout tracking API built with FastAPI.

## Features

- Log weight / sleep / condition (1 per day, upsert)
- Log workouts (multiple per day)
- Day summary endpoint
- Simple chart dashboard at `/charts`

## Tech Stack

- FastAPI
- SQLModel (SQLite)
- Uvicorn
- Chart.js

## Run Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload