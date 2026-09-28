from fastapi import FastAPI
from .routers import auth, rooms, requests

app = FastAPI(title="CampusReserve")

app.include_router(auth.router)
app.include_router(rooms.router)
app.include_router(requests.router)
