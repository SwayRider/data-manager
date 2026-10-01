from flask import Blueprint, render_template

bp = Blueprint(
    "deploy", __name__, url_prefix="/deploy", template_folder="templates"
)


@bp.get("/")
def index():
    return render_template("deploy/index.html")
