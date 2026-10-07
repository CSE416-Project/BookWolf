"""Dev-only runner for the rooms router, with no login and no database.
Not for production. For the full app, use `uvicorn main:app --reload`.

Run from the server/ folder:
    uvicorn dev_rooms:app --reload
Then open http://127.0.0.1:8000/docs (shows 25Live data only).
"""

from fastapi import FastAPI

from authentication import get_current_user
from database import get_db
from routers import rooms

app = FastAPI(title="CampusReserve rooms (dev, 25Live only)")
app.include_router(rooms.router)

app.dependency_overrides[get_current_user] = lambda: None
app.dependency_overrides[get_db] = lambda: None
rooms._room_list_data = lambda db: ({}, {}, {})
rooms._schedule_data = lambda db, room_id, start, end: (None, [], [], [])
