import sys
from flask import Flask, jsonify
import Crypto

app = Flask(__name__)


@app.route("/")
def hello():
    return jsonify({"message": "Hello World"})


@app.route("/version")
def version():
    import importlib.metadata
    lib_version = getattr(Crypto, '__version__', None) or __import__('importlib.metadata', fromlist=['version']).version('pycryptodome')
    return jsonify({
        "language": {"name": "Python", "version": sys.version.split()[0]},
        "framework": {"name": "Flask", "version": importlib.metadata.version("flask")},
        "library": {"name": "PyCryptodome", "version": str(lib_version)},
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
