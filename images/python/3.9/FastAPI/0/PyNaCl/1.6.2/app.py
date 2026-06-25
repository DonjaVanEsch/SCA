import sys
import uvicorn
import fastapi
from fastapi import FastAPI
import nacl

app = FastAPI()


@app.get("/")
def hello():
    return {"message": "Hello World"}


@app.get("/version")
def version():
    lib_version = nacl.__version__
    return {
        "language": {"name": "Python", "version": sys.version.split()[0]},
        "framework": {"name": "FastAPI", "version": fastapi.__version__},
        "library": {"name": "PyNaCl", "version": str(lib_version)},
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
