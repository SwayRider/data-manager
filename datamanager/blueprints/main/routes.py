from flask import Blueprint, redirect, url_for

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    return redirect(url_for("configure.index"))
