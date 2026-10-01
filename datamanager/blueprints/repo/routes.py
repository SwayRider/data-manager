from flask import Blueprint, render_template

bp = Blueprint(
    "repo", __name__, url_prefix="/repo", template_folder="templates"
)


@bp.get("/")
def index():
    return render_template("repo/index.html")
