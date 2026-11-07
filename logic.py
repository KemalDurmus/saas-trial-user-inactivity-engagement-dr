"""Core logic: find trial users with no recent activity. Pure Python, no Flask."""
import csv
import io
import datetime as dt
from urllib.parse import quote

MAX_ROWS = 50000
MAX_BYTES = 5 * 1024 * 1024

EMAIL_ALIASES = ["email", "user_email", "email_address", "user_id", "userid", "user", "account_email", "id"]
SIGNUP_ALIASES = ["signup_date", "signed_up_at", "signup_at", "signup", "created_at", "created", "trial_start", "trial_started_at", "date"]
EVENT_DATE_ALIASES = ["timestamp", "event_time", "occurred_at", "event_date", "last_seen", "last_active", "created_at", "time", "date"]
MRR_ALIASES = ["mrr", "monthly_revenue", "plan_price", "price", "mrr_value", "amount", "monthly_price"]


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def decode_bytes(raw, label):
    if not raw or not raw.strip():
        raise ValueError(f"The {label} file is empty.")
    if b"\x00" in raw[:8192]:
        raise ValueError(f"The {label} file doesn't look like text (it seems to be a binary file). Please upload a CSV export.")
    text = raw.decode("utf-8-sig", errors="replace")
    if text.count("�") > max(5, len(text) // 50):
        raise ValueError(f"The {label} file doesn't look like readable text. Please upload a UTF-8 CSV export.")
    return text


def _norm(name):
    return (name or "").strip().lower().replace(" ", "_").replace("-", "_")


def read_rows(text, label, warnings):
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError(f"The {label} data is empty.")
    lines = text.split("\n")
    header = lines[0]
    delim = max([",", ";", "\t"], key=lambda d: header.count(d))
    if header.count(delim) == 0:
        raise ValueError(f"The {label} data has no columns. Expected a CSV with a header row separated by commas.")
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        fields = [_norm(h) for h in next(reader)]
    except StopIteration:
        raise ValueError(f"The {label} data is empty.")
    rows = []
    truncated = False
    try:
        for rec in reader:
            if not any((c or "").strip() for c in rec):
                continue
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
            rows.append({fields[i]: (rec[i].strip() if i < len(rec) else "") for i in range(len(fields))})
    except csv.Error as exc:
        raise ValueError(f"The {label} data could not be parsed as CSV: {exc}")
    if truncated:
        warnings.append(f"The {label} data has more than {MAX_ROWS:,} rows. Only the first {MAX_ROWS:,} rows were analyzed.")
    if not rows:
        raise ValueError(f"The {label} data has a header but no readable rows.")
    return fields, rows


def find_col(fields, aliases):
    for a in aliases:
        if a in fields:
            return a
    return None


def parse_date(value):
    s = (value or "").strip()
    if not s:
        return None
    if s.isdigit() and len(s) in (10, 13):
        try:
            n = int(s) / (1000 if len(s) == 13 else 1)
            return dt.datetime.fromtimestamp(n, dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        return dt.datetime.fromisoformat(iso).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%b %d, %Y", "%B %d, %Y",
                "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_money(value):
    s = (value or "").replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return n if n >= 0 else None


def _csv_safe(v):
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def analyze(signup_text, activity_text, window_days, trial_days, default_mrr, today=None):
    today = today or dt.date.today()
    warnings = []
    s_fields, s_rows = read_rows(signup_text, "signups", warnings)
    a_fields, a_rows = read_rows(activity_text, "activity", warnings)

    s_email = find_col(s_fields, EMAIL_ALIASES)
    s_date = find_col(s_fields, SIGNUP_ALIASES)
    if not s_email or not s_date:
        raise ValueError("The signups CSV needs an email (or user_id) column and a signup date column (e.g. signup_date). Found columns: " + ", ".join(f for f in s_fields if f) + ".")
    a_email = find_col(a_fields, EMAIL_ALIASES)
    a_date = find_col(a_fields, EVENT_DATE_ALIASES)
    if not a_email or not a_date:
        raise ValueError("The activity CSV needs an email (or user_id) column and a timestamp column (e.g. timestamp). Found columns: " + ", ".join(f for f in a_fields if f) + ".")
    mrr_col = find_col(s_fields, MRR_ALIASES)

    users = {}
    bad_dates = 0
    for r in s_rows:
        key = r.get(s_email, "").strip().lower()
        d = parse_date(r.get(s_date, ""))
        if not key or d is None:
            bad_dates += 1
            continue
        mrr = parse_money(r.get(mrr_col, "")) if mrr_col else None
        if key in users:
            if d < users[key]["signup"]:
                users[key]["signup"] = d
            continue
        users[key] = {"email": r.get(s_email, "").strip(), "signup": d, "mrr": mrr}
    if not users:
        raise ValueError("No signup rows could be read. Check that the signup date column contains dates like 2026-10-31.")
    if bad_dates:
        warnings.append(f"{plural(bad_dates, 'signup row')} skipped because the email or date was missing or unreadable.")

    last = {}
    valid_events = 0
    bad_events = 0
    unmatched = set()
    for r in a_rows:
        key = r.get(a_email, "").strip().lower()
        d = parse_date(r.get(a_date, ""))
        if not key or d is None:
            bad_events += 1
            continue
        valid_events += 1
        if key not in users:
            unmatched.add(key)
            continue
        if key not in last or d > last[key]:
            last[key] = d
    if valid_events == 0:
        raise ValueError("No activity rows could be read. Check that the timestamp column contains dates like 2026-10-31 or 2026-10-31T14:05:00Z.")
    if bad_events:
        warnings.append(f"{plural(bad_events, 'activity row')} skipped because the email or timestamp was missing or unreadable.")
    if unmatched:
        warnings.append(f"{plural(len(unmatched), 'user')} in the activity file did not match any signup and were ignored.")
    if not last:
        warnings.append("None of the activity rows matched a signup email. Make sure both files use the same email or user ID values.")

    cutoff = today - dt.timedelta(days=window_days)
    rows = []
    expired = 0
    active = 0
    for key, u in users.items():
        trial_end = u["signup"] + dt.timedelta(days=trial_days)
        if trial_end < today:
            expired += 1
            continue
        la = last.get(key)
        if la is not None and la >= cutoff:
            active += 1
            continue
        mrr = u["mrr"] if u["mrr"] is not None else default_mrr
        rows.append({
            "email": u["email"],
            "signup": u["signup"].isoformat(),
            "trial_end": trial_end.isoformat(),
            "days_left": (trial_end - today).days,
            "last_activity": la.isoformat() if la else "Never",
            "days_inactive": str(max((today - la).days, 0)) if la else "No activity",
            "mrr": mrr,
            "mrr_text": f"${mrr:,.2f}",
        })
    rows.sort(key=lambda x: (-x["mrr"], x["days_left"], x["email"].lower()))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    total = sum(r["mrr"] for r in rows)
    never = sum(1 for r in rows if r["last_activity"] == "Never")

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["rank", "email", "signup_date", "trial_end", "days_left", "last_activity", "days_inactive", "potential_mrr"])
    for r in rows:
        w.writerow([r["rank"], _csv_safe(r["email"]), r["signup"], r["trial_end"], r["days_left"],
                    r["last_activity"], r["days_inactive"], f"{r['mrr']:.2f}"])
    export_csv = out.getvalue()

    return {
        "rows": rows,
        "inactive_count": len(rows),
        "active_count": active,
        "expired_count": expired,
        "never_count": never,
        "total_mrr": f"${total:,.2f}",
        "trials_checked": len(rows) + active,
        "used_mrr_column": bool(mrr_col),
        "warnings": warnings,
        "data_uri": "data:text/csv;charset=utf-8," + quote(export_csv),
        "today": today.isoformat(),
    }


def demo_data(today=None):
    today = today or dt.date.today()
    people = [
        ("anna@acme.io", 12, "99", 9),
        ("ben@northwind.co", 10, "49", 8),
        ("carla@bloomhq.com", 5, "199", None),
        ("dev@pixelforge.dev", 13, "49", 11),
        ("elif@lumen.app", 3, "29", 0),
        ("farid@kitedata.io", 9, "99", 2),
        ("gina@oakleaf.co", 11, "199", 10),
        ("hugo@brightly.dev", 6, "29", 1),
        ("ivy@tallship.io", 20, "49", 18),
        ("jon@mintly.app", 8, "29", 7),
        ("kai@sparkly.dev", 13, "99", None),
    ]
    s = ["email,signup_date,plan,mrr"]
    a = ["email,event,timestamp"]
    plans = {"29": "Starter", "49": "Growth", "99": "Pro", "199": "Business"}
    for email, ago, mrr, last_ago in people:
        s.append(f"{email},{(today - dt.timedelta(days=ago)).isoformat()},{plans[mrr]},{mrr}")
        if last_ago is not None:
            a.append(f"{email},login,{(today - dt.timedelta(days=ago)).isoformat()}T09:15:00Z")
            a.append(f"{email},open_dashboard,{(today - dt.timedelta(days=last_ago)).isoformat()}T16:40:00Z")
    return "\n".join(s), "\n".join(a)
