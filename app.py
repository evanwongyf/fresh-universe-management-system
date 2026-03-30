from flask import Flask, render_template, request, redirect, url_for, session, flash
import os, re, hashlib, secrets
from functools import wraps
from dotenv import load_dotenv
load_dotenv()
from r2_storage import (
    ensure_project_folder,
    load_interviews_from_r2,
    load_blog_posts_from_r2,
    upload_interview_source,
    upload_blog_post_source,
    delete_interview,
    delete_blog_post,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ---------------------------------------------------------------------------
# Simple single-user auth (stored in env or hardcoded fallback for dev)
# In production set CMS_USERNAME / CMS_PASSWORD_HASH env vars.
# Generate hash: python -c "import hashlib; print(hashlib.sha256(b'yourpassword').hexdigest())"
# ---------------------------------------------------------------------------
USERS_FILE = os.path.join(os.path.dirname(__file__), "users.txt")

def _load_users():
    """Load users from users.txt as {username: password_hash}."""
    users = {}
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            for line in f:
                line = line.strip()
                if ":" in line:
                    u, h = line.split(":", 1)
                    users[u.strip()] = h.strip()
    return users

def _save_user(username, password_hash):
    with open(USERS_FILE, "a") as f:
        f.write(f"{username}:{password_hash}\n")

def _hash(password):
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if session.get("user"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        users = _load_users()
        if username in users and users[username] == _hash(password):
            session["user"] = username
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        users = _load_users()
        if not username or not password:
            flash("Username and password are required.", "error")
        elif username in users:
            flash("Username already taken.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            _save_user(username, _hash(password))
            session["user"] = username
            flash("Account created!", "success")
            return redirect(url_for("dashboard"))
    return render_template("signup.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    interviews = load_interviews_from_r2()
    posts      = load_blog_posts_from_r2()
    return render_template("dashboard.html", interviews=interviews, posts=posts)

# ---------------------------------------------------------------------------
# Interviews
# ---------------------------------------------------------------------------
@app.route("/interviews")
@login_required
def interviews():
    items = load_interviews_from_r2()
    return render_template("interviews.html", interviews=items)

@app.route("/interviews/new", methods=["GET", "POST"])
@login_required
def interview_new():
    if request.method == "POST":
        data, error = _parse_interview_form(request.form)
        if error:
            flash(error, "error")
            return render_template("interview_form.html", mode="new", form=request.form)
        source = _render_interview_source(data)
        filename = _slugify(data["title"]) + ".py"
        upload_interview_source(filename, source)
        flash(f"Interview "{data['title']}" saved.", "success")
        return redirect(url_for("interviews"))
    return render_template("interview_form.html", mode="new", form={})

@app.route("/interviews/edit/<slug>", methods=["GET", "POST"])
@login_required
def interview_edit(slug):
    all_ivs = load_interviews_from_r2()
    iv = next((i for i in all_ivs if i["slug"] == slug), None)
    if not iv:
        flash("Interview not found.", "error")
        return redirect(url_for("interviews"))

    if request.method == "POST":
        data, error = _parse_interview_form(request.form)
        if error:
            flash(error, "error")
            return render_template("interview_form.html", mode="edit", form=request.form, interview=iv)
        source = _render_interview_source(data)
        filename = _slugify(data["title"]) + ".py"
        # Remove old file if slug/title changed
        old_filename = iv.get("_filename") or (_slugify(iv["title"]) + ".py")
        if old_filename != filename:
            delete_interview(old_filename)
        upload_interview_source(filename, source)
        flash(f"Interview "{data['title']}" updated.", "success")
        return redirect(url_for("interviews"))

    # pre-populate form from existing data
    form = dict(iv)
    form["questions_json"] = iv.get("questions", [])
    return render_template("interview_form.html", mode="edit", form=form, interview=iv)

@app.route("/interviews/delete/<slug>", methods=["POST"])
@login_required
def interview_delete(slug):
    all_ivs = load_interviews_from_r2()
    iv = next((i for i in all_ivs if i["slug"] == slug), None)
    if iv:
        filename = iv.get("_filename") or (_slugify(iv["title"]) + ".py")
        delete_interview(filename)
        flash("Interview deleted.", "success")
    return redirect(url_for("interviews"))

# ---------------------------------------------------------------------------
# Blog posts
# ---------------------------------------------------------------------------
@app.route("/blog")
@login_required
def blog():
    posts = load_blog_posts_from_r2()
    return render_template("blog.html", posts=posts)

@app.route("/blog/new", methods=["GET", "POST"])
@login_required
def blog_new():
    if request.method == "POST":
        data, error = _parse_blog_form(request.form)
        if error:
            flash(error, "error")
            return render_template("blog_form.html", mode="new", form=request.form)
        source = _render_blog_source(data)
        filename = _slugify(data["title"]) + ".py"
        upload_blog_post_source(filename, source)
        flash(f"Post "{data['title']}" saved.", "success")
        return redirect(url_for("blog"))
    return render_template("blog_form.html", mode="new", form={})

@app.route("/blog/edit/<slug>", methods=["GET", "POST"])
@login_required
def blog_edit(slug):
    all_posts = load_blog_posts_from_r2()
    post = next((p for p in all_posts if p["slug"] == slug), None)
    if not post:
        flash("Post not found.", "error")
        return redirect(url_for("blog"))

    if request.method == "POST":
        data, error = _parse_blog_form(request.form)
        if error:
            flash(error, "error")
            return render_template("blog_form.html", mode="edit", form=request.form, post=post)
        source = _render_blog_source(data)
        filename = _slugify(data["title"]) + ".py"
        old_filename = post.get("_filename") or (_slugify(post["title"]) + ".py")
        if old_filename != filename:
            delete_blog_post(old_filename)
        upload_blog_post_source(filename, source)
        flash(f"Post "{data['title']}" updated.", "success")
        return redirect(url_for("blog"))

    return render_template("blog_form.html", mode="edit", form=post, post=post)

@app.route("/blog/delete/<slug>", methods=["POST"])
@login_required
def blog_delete(slug):
    all_posts = load_blog_posts_from_r2()
    post = next((p for p in all_posts if p["slug"] == slug), None)
    if post:
        filename = post.get("_filename") or (_slugify(post["title"]) + ".py")
        delete_blog_post(filename)
        flash("Post deleted.", "success")
    return redirect(url_for("blog"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text

def _parse_interview_form(form):
    title    = form.get("title", "").strip()
    slug     = form.get("slug", "").strip() or _slugify(title)
    category = form.get("category", "").strip()
    date     = form.get("date", "").strip()
    iv_id    = form.get("iv_id", "").strip()

    if not title: return None, "Title is required."
    if not date:  return None, "Date is required."

    # questions: question_0, answer_0, question_1, answer_1 ...
    questions = []
    i = 0
    while True:
        q = form.get(f"question_{i}", "").strip()
        a = form.get(f"answer_{i}", "").strip()
        if not q and not a:
            break
        if q or a:
            questions.append({"question": q, "answer": a})
        i += 1

    return {"title": title, "slug": slug, "category": category,
            "date": date, "id": iv_id, "questions": questions}, None

def _parse_blog_form(form):
    title    = form.get("title", "").strip()
    slug     = form.get("slug", "").strip() or _slugify(title)
    author   = form.get("author", "").strip()
    bio      = form.get("bio", "").strip()
    date     = form.get("date", "").strip()
    content  = form.get("content", "")
    category = form.get("category", "").strip()

    if not title:  return None, "Title is required."
    if not date:   return None, "Date is required."
    if not author: return None, "Author is required."

    return {"title": title, "slug": slug, "author": author, "bio": bio,
            "date": date, "content": content, "category": category}, None

def _render_interview_source(data):
    """Render a valid Python source string for an interview dict."""
    import json
    lines = [
        f'interview = {{',
        f'    "id": {json.dumps(data["id"])},',
        f'    "slug": {json.dumps(data["slug"])},',
        f'    "category": {json.dumps(data["category"])},',
        f'    "date": {json.dumps(data["date"])},',
        f'    "title": {json.dumps(data["title"])},',
        f'    "questions": [',
    ]
    for q in data["questions"]:
        lines.append(f'        {{')
        lines.append(f'            "question": {json.dumps(q["question"])},')
        lines.append(f'            "answer": {json.dumps(q["answer"])},')
        lines.append(f'        }},')
    lines.append(f'    ]')
    lines.append(f'}}')
    return "\n".join(lines) + "\n"

def _render_blog_source(data):
    """Render a valid Python source string for a blog post dict."""
    import json
    # Use triple-quoted string for content to preserve all HTML/whitespace
    content_escaped = data["content"].replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    lines = [
        f'post = {{',
        f'    "title": {json.dumps(data["title"])},',
        f'    "slug": {json.dumps(data["slug"])},',
        f'    "author": {json.dumps(data["author"])},',
        f'    "bio": {json.dumps(data["bio"])},',
        f'    "date": {json.dumps(data["date"])},',
        f'    "content": """{content_escaped}""",',
        f'    "category": {json.dumps(data["category"])},',
        f'}}',
    ]
    return "\n".join(lines) + "\n"

if __name__ == "__main__":
    ensure_project_folder()
    app.run(debug=True, port=5001)
