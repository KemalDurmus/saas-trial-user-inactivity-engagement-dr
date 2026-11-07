from flask import Blueprint, render_template, request, flash

import logic

bp = Blueprint("feature", __name__)


def _int(name, default, lo, hi):
    try:
        v = int(float((request.form.get(name) or "").strip()))
    except (ValueError, TypeError):
        return default
    return max(lo, min(hi, v))


def _money(name, default):
    try:
        v = float((request.form.get(name) or "").replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return default
    return max(0.0, min(1000000.0, v))


def _read_upload(field, label, warnings):
    f = request.files.get(field)
    if not f or not f.filename:
        return None
    raw = f.read(logic.MAX_BYTES + 1)
    if len(raw) > logic.MAX_BYTES:
        raw = raw[:logic.MAX_BYTES]
        cut = raw.rfind(b"\n")
        if cut > 0:
            raw = raw[:cut]
        warnings.append(f"The {label} file is larger than 5 MB. Only the first 5 MB were analyzed.")
    return logic.decode_bytes(raw, label)


@bp.route("/")
def index():
    return render_template("index.html", result=None, form={"window_days": 7, "trial_days": 14, "default_mrr": "49",
                                                            "signups_text": "", "activity_text": ""})


@bp.route("/analyze", methods=["POST"])
def analyze():
    form = {
        "window_days": _int("window_days", 7, 1, 90),
        "trial_days": _int("trial_days", 14, 1, 365),
        "default_mrr": request.form.get("default_mrr") or "49",
        "signups_text": request.form.get("signups_text") or "",
        "activity_text": request.form.get("activity_text") or "",
    }
    result = None
    try:
        warnings = []
        if request.form.get("demo"):
            form["signups_text"], form["activity_text"] = logic.demo_data()
            form["window_days"], form["trial_days"] = 7, 14
            signups, activity = form["signups_text"], form["activity_text"]
        else:
            signups = _read_upload("signups_file", "signups", warnings)
            activity = _read_upload("activity_file", "activity", warnings)
            if signups is None:
                signups = form["signups_text"]
            if activity is None:
                activity = form["activity_text"]
            if not signups.strip() and not activity.strip():
                raise ValueError("Please upload or paste both CSV files, or click “Try with sample data”.")
            if not signups.strip():
                raise ValueError("The signups CSV is missing. Upload or paste it.")
            if not activity.strip():
                raise ValueError("The activity CSV is missing. Upload or paste it.")
        default_mrr = _money("default_mrr", 49.0)
        result = logic.analyze(signups, activity, form["window_days"], form["trial_days"], default_mrr)
        result["warnings"] = warnings + result["warnings"]
        for w in result["warnings"]:
            flash(w, "warning")
        if request.form.get("demo"):
            form["signups_text"] = form["activity_text"] = ""
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception:
        flash("Something went wrong while reading your data. Please check that both files are valid CSV exports.", "error")
    return render_template("index.html", result=result, form=form, plural=logic.plural,
                           demo=bool(request.form.get("demo")))
