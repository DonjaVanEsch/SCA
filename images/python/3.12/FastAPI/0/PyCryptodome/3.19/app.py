import sys
import uvicorn
import fastapi
from fastapi import FastAPI
import Crypto

app = FastAPI()


@app.get("/")
def hello():
    return {"message": "Hello World"}


@app.get("/version")
def version():
    lib_version = getattr(Crypto, '__version__', None) or __import__('importlib.metadata', fromlist=['version']).version('pycryptodome')
    return {
        "language": {"name": "Python", "version": sys.version.split()[0]},
        "framework": {"name": "FastAPI", "version": fastapi.__version__},
        "library": {"name": "PyCryptodome", "version": str(lib_version)},
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
