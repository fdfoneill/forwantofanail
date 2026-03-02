from fastapi import FastAPI

from forwantofanail.api.routes import router

app = FastAPI(title="For Want of a Nail API", version="0.1.1")
app.include_router(router)
