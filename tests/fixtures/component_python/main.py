from fastapi import FastAPI
from routes.orders import router

app = FastAPI()
app.include_router(router)
