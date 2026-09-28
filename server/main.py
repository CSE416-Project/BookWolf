from fastapi import FastAPI
from .routers import auth

app = FastAPI(title="CampusReserve")

app.include_router(auth.router)
