import sys
from flask import Flask, jsonify
import M2Crypto

app = Flask(__name__)


@app.route("/")
def hello():
    return jsonify({"message": "Hello World"})


@app.route("/version")
def version():
    import flask
    lib_version = M2Crypto.version
    return jsonify({
        "language": {"name": "Python", "version": sys.version.split()[0]},
        "framework": {"name": "Flask", "version": flask.__version__},
        "library": {"name": "M2Crypto", "version": str(lib_version)},
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
