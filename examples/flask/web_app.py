from flask import Flask, request

from appguard_flask import AppGuard
from service import answer


app = Flask(__name__)


@app.before_request
def check_license():
    pass


@app.get("/")
def index():
    return {"status": "ok"}


@app.get("/api/answer")
def calculate():
    return {"answer": answer(request.args.get("value", 0, type=int))}


AppGuard().init_app(app)
