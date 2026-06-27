import sys
from flask import Flask, jsonify
import cryptography

app = Flask(__name__)


@app.route("/")
def hello():
    return jsonify({"message": "Hello World"})


@app.route("/version")
def version():
    import importlib.metadata
    lib_version = cryptography.__version__
    return jsonify({
        "language": {"name": "Python", "version": sys.version.split()[0]},
        "framework": {"name": "Flask", "version": importlib.metadata.version("flask")},
        "library": {"name": "cryptography", "version": str(lib_version)},
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
