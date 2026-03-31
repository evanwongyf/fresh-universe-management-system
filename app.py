from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
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
    load_issues_from_r2,
    upload_issue_source,
    delete_issue,
    load_staff_from_r2,
    save_staff_to_r2,
    load_staff_applications_from_r2,
    save_staff_applications_to_r2,
    load_site_settings, save_site_settings,
    load_home_config, save_home_config,
    load_about_config, save_about_config,
    load_submissions_config, save_submissions_config,
    load_blog_page_config, save_blog_page_config,
    load_interviews_page_config, save_interviews_page_config,
    _fetch_source,
    upload_image_to_r2,
    R2_PUBLIC_BASE_URL,
    load_form_config, save_form_config,
    load_submissions, delete_submission,
    R2_PROJECT_FOLDER, R2_BUCKET_NAME, _client,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.jinja_env.filters['enumerate'] = enumerate

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
    interviews  = load_interviews_from_r2()
    posts       = load_blog_posts_from_r2()
    issues      = load_issues_from_r2()
    members     = load_staff_from_r2()
    apps_config = load_staff_applications_from_r2()
    return render_template("dashboard.html", interviews=interviews, posts=posts,
                           issues=issues, members=members, apps_config=apps_config)

# ---------------------------------------------------------------------------
# Interviews
# ---------------------------------------------------------------------------
@app.route("/interviews")
@login_required
def interviews():
    try:
        items = load_interviews_from_r2()
    except Exception as e:
        flash(f"Could not load interviews from R2: {e}", "error")
        items = []
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
        flash(f"Interview '{data['title']}' saved.", "success")
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
        flash(f"Interview '{data['title']}' updated.", "success")
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
    try:
        posts = load_blog_posts_from_r2()
    except Exception as e:
        flash(f"Could not load blog posts from R2: {e}", "error")
        posts = []
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
        flash(f"Post '{data['title']}' saved.", "success")
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
        flash(f"Post '{data['title']}' updated.", "success")
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
    image    = form.get("image", "").strip()

    if not title:  return None, "Title is required."
    if not date:   return None, "Date is required."
    if not author: return None, "Author is required."

    return {"title": title, "slug": slug, "author": author, "bio": bio,
            "date": date, "content": content, "category": category, "image": image}, None

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
        f'    "image": {json.dumps(data.get("image", ""))},',
        f'}}',
    ]
    return "\n".join(lines) + "\n"

# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------
@app.route("/issues")
@login_required
def issues():
    try:
        items = load_issues_from_r2()
    except Exception as e:
        flash(f"Could not load issues from R2: {e}", "error")
        items = []
    return render_template("issues.html", issues=items)

@app.route("/issues/new", methods=["GET", "POST"])
@login_required
def issue_new():
    if request.method == "POST":
        data, error = _parse_issue_form(request.form)
        if error:
            flash(error, "error")
            return render_template("issue_form.html", mode="new", form=request.form)
        source = _render_issue_source(data)
        filename = _slugify(data["issue_name"]) + ".py"
        upload_issue_source(filename, source)
        flash(f"Issue '{data['issue_name']}' saved.", "success")
        return redirect(url_for("issues"))
    return render_template("issue_form.html", mode="new", form={})

@app.route("/issues/edit/<slug>", methods=["GET", "POST"])
@login_required
def issue_edit(slug):
    all_issues = load_issues_from_r2()
    issue = next((i for i in all_issues if i["slug"] == slug), None)
    if not issue:
        flash("Issue not found.", "error")
        return redirect(url_for("issues"))
    if request.method == "POST":
        data, error = _parse_issue_form(request.form)
        if error:
            flash(error, "error")
            return render_template("issue_form.html", mode="edit", form=request.form, issue=issue)
        source = _render_issue_source(data)
        filename = _slugify(data["issue_name"]) + ".py"
        old_filename = issue.get("_filename") or (_slugify(issue["issue_name"]) + ".py")
        if old_filename != filename:
            delete_issue(old_filename)
        upload_issue_source(filename, source)
        flash(f"Issue '{data['issue_name']}' updated.", "success")
        return redirect(url_for("issues"))
    return render_template("issue_form.html", mode="edit", form=issue, issue=issue)

@app.route("/issues/delete/<slug>", methods=["POST"])
@login_required
def issue_delete(slug):
    all_issues = load_issues_from_r2()
    issue = next((i for i in all_issues if i["slug"] == slug), None)
    if issue:
        filename = issue.get("_filename") or (_slugify(issue["issue_name"]) + ".py")
        delete_issue(filename)
        flash("Issue deleted.", "success")
    return redirect(url_for("issues"))

# ---------------------------------------------------------------------------
# Masthead
# ---------------------------------------------------------------------------
@app.route("/masthead")
@login_required
def masthead():
    return render_template("masthead.html", members=load_staff_from_r2())

@app.route("/masthead/new", methods=["GET", "POST"])
@login_required
def masthead_new():
    if request.method == "POST":
        data, error = _parse_member_form(request.form)
        if error:
            flash(error, "error")
            return render_template("masthead_form.html", mode="new", form=request.form)
        members = load_staff_from_r2()
        members.append(data)
        save_staff_to_r2(members)
        flash(f"'{data['name']}' added to masthead.", "success")
        return redirect(url_for("masthead"))
    return render_template("masthead_form.html", mode="new", form={})

@app.route("/masthead/edit/<slug>", methods=["GET", "POST"])
@login_required
def masthead_edit(slug):
    members = load_staff_from_r2()
    member = next((m for m in members if m["slug"] == slug), None)
    if not member:
        flash("Member not found.", "error")
        return redirect(url_for("masthead"))
    if request.method == "POST":
        data, error = _parse_member_form(request.form)
        if error:
            flash(error, "error")
            return render_template("masthead_form.html", mode="edit", form=request.form, member=member)
        updated = [data if m["slug"] == slug else m for m in members]
        save_staff_to_r2(updated)
        flash(f"'{data['name']}' updated.", "success")
        return redirect(url_for("masthead"))
    return render_template("masthead_form.html", mode="edit", form=member, member=member)

@app.route("/masthead/delete/<slug>", methods=["POST"])
@login_required
def masthead_delete(slug):
    members = load_staff_from_r2()
    updated = [m for m in members if m["slug"] != slug]
    save_staff_to_r2(updated)
    flash("Member removed.", "success")
    return redirect(url_for("masthead"))

# ---------------------------------------------------------------------------
# Staff Applications
# ---------------------------------------------------------------------------
_DEFAULT_APPS = {
    "is_open": True,
    "heading": "Staff Applications are currently open.",
    "description": "We're looking for passionate individuals from all walks of life and of any nationality to join our team. Whether you're a writer, editor, designer, or social media enthusiast, we have a role for you. Experience is appreciated, but not compulsory.",
    "apply_link": "https://forms.gle/AsbyCx5SxpXANpNBA",
    "roles": [
        {"name": "Literary Editors", "description": "Literary Editors review and edit written submissions, ensuring clarity, coherence, and alignment with our magazine's vision."},
        {"name": "Media Editors", "description": "Media Editors oversee visual and multimedia content, including artwork, photography, and videos."},
        {"name": "Graphic Designers", "description": "Graphic Designers create visual assets for our magazine and digital platforms."},
        {"name": "Staff Writers", "description": "Staff Writers produce original written content for our magazine and blog."},
        {"name": "Staff Artists", "description": "Staff Artists contribute original artwork to accompany articles."},
        {"name": "Outreach Managers", "description": "Outreach Managers build partnerships and expand our magazine's reach."},
        {"name": "Interviewers", "description": "Interviewers conduct engaging interviews with artists, writers, and creatives."},
        {"name": "Social Media Managers", "description": "Social Media Managers handle our online presence across platforms."},
        {"name": "Blog Managers", "description": "Blog Managers oversee our blog, coordinating with writers to publish regular posts."},
        {"name": "Email Managers", "description": "Email Managers handle our email communications and newsletters."},
        {"name": "Reel Creators", "description": "Reel Creators produce short, engaging video content for our social media platforms."},
    ],
}

@app.route("/staff-applications", methods=["GET", "POST"])
@login_required
def staff_applications():
    config = load_staff_applications_from_r2() or _DEFAULT_APPS
    if request.method == "POST":
        is_open = request.form.get("is_open") == "1"
        heading     = request.form.get("heading", "").strip()
        description = request.form.get("description", "").strip()
        apply_link  = request.form.get("apply_link", "").strip()
        roles = []
        i = 0
        while True:
            name = request.form.get(f"role_name_{i}", "").strip()
            desc = request.form.get(f"role_desc_{i}", "").strip()
            if not name and not desc:
                break
            if name:
                roles.append({"name": name, "description": desc})
            i += 1
        config = {"is_open": is_open, "heading": heading, "description": description,
                  "apply_link": apply_link, "roles": roles}
        save_staff_applications_to_r2(config)
        flash("Staff applications page updated.", "success")
        return redirect(url_for("staff_applications"))
    return render_template("staff_applications.html", config=config)

# ---------------------------------------------------------------------------
# Issue / member form helpers
# ---------------------------------------------------------------------------
def _parse_issue_form(form):
    issue_name   = form.get("issue_name", "").strip()
    slug         = form.get("slug", "").strip() or _slugify(issue_name)
    date_pub     = form.get("date_published", "").strip()
    description  = form.get("description", "").strip()
    staff        = form.get("staff", "").strip()
    statistics   = form.get("statistics", "").strip()
    read_url     = form.get("read_url", "").strip()
    heyzine_id   = form.get("heyzine_id", "").strip()
    if not issue_name: return None, "Issue name is required."
    if not date_pub:   return None, "Date is required."
    return {"issue_name": issue_name, "slug": slug, "date_published": date_pub,
            "description": description, "staff": staff, "statistics": statistics,
            "read_url": read_url, "heyzine_id": heyzine_id}, None

def _render_issue_source(data):
    import json
    return f"""issue = {{
    "slug": {json.dumps(data["slug"])},
    "issue_name": {json.dumps(data["issue_name"])},
    "date_published": {json.dumps(data["date_published"])},
    "description": {json.dumps(data["description"])},
    "heyzine_id": {json.dumps(data.get("heyzine_id", ""))},
    "read_url": {json.dumps(data.get("read_url", ""))},
    "staff": {json.dumps(data["staff"])},
    "statistics": {json.dumps(data["statistics"])},
}}
"""

def _parse_member_form(form):
    name  = form.get("name", "").strip()
    slug  = form.get("slug", "").strip() or _slugify(name)
    role  = form.get("role", "").strip()
    image = form.get("image", "").strip()
    bio   = form.get("bio", "").strip()
    if not name: return None, "Name is required."
    if not role: return None, "Role is required."
    return {"name": name, "slug": slug, "role": role, "image": image, "bio": bio}, None


# ---------------------------------------------------------------------------
# Site Settings
# ---------------------------------------------------------------------------
_DEFAULT_SETTINGS = {
    "instagram_url": "https://www.instagram.com/freshuniverse.mag",
    "contact_email": "freshuniversemagazine@gmail.com",
    "footer_brand": "Fresh Universe",
}

@app.route("/settings", methods=["GET", "POST"])
@login_required
def site_settings():
    config = load_site_settings() or _DEFAULT_SETTINGS
    if request.method == "POST":
        config = {
            "instagram_url": request.form.get("instagram_url", "").strip(),
            "contact_email": request.form.get("contact_email", "").strip(),
            "footer_brand":  request.form.get("footer_brand", "").strip(),
        }
        save_site_settings(config)
        flash("Site settings saved.", "success")
        return redirect(url_for("site_settings"))
    return render_template("site_settings.html", config=config)

# ---------------------------------------------------------------------------
# Home page
# ---------------------------------------------------------------------------
_DEFAULT_HOME = {
    "welcome_heading": "Welcome",
    "welcome_text": "Fresh Universe Magazine is a vibrant platform dedicated to showcasing the creative talents of all ages and nationalities. We publish original works of literature, art, photography, and pretty much anything remotely creative! We provide an all-encompassing space for emerging and established creators to share their voices with the world. Our mission is to inspire, connect, and empower creatives through a supportive and inclusive community.",
    "banner_items": "Literature,Art,Photography,Music,Experimental,Everything",
}

@app.route("/pages/home", methods=["GET", "POST"])
@login_required
def page_home():
    config = load_home_config() or _DEFAULT_HOME
    if request.method == "POST":
        config = {
            "welcome_heading": request.form.get("welcome_heading", "").strip(),
            "welcome_text":    request.form.get("welcome_text", "").strip(),
            "banner_items":    request.form.get("banner_items", "").strip(),
        }
        save_home_config(config)
        flash("Home page saved.", "success")
        return redirect(url_for("page_home"))
    return render_template("page_home.html", config=config)

# ---------------------------------------------------------------------------
# About Us page
# ---------------------------------------------------------------------------
_DEFAULT_ABOUT = {
    "heading":     "A canvas of fresh voices",
    "description": "Fresh Universe Magazine is a vibrant platform dedicated to showcasing the creative talents of individuals from all walks of life, everywhere! We publish original works of literature, art, photography, experimental work, and other media types. We hope to provide a space for emerging creators to share their voices with the world. Our mission is to inspire, connect, and empower the next generation of artists and storytellers through a supportive and inclusive community.",
}

@app.route("/pages/about", methods=["GET", "POST"])
@login_required
def page_about():
    config = load_about_config() or _DEFAULT_ABOUT
    if request.method == "POST":
        config = {
            "heading":     request.form.get("heading", "").strip(),
            "description": request.form.get("description", "").strip(),
        }
        save_about_config(config)
        flash("About Us page saved.", "success")
        return redirect(url_for("page_about"))
    return render_template("page_about.html", config=config)

# ---------------------------------------------------------------------------
# Submissions page
# ---------------------------------------------------------------------------
_DEFAULT_SUBMISSIONS = {
    "is_open":     True,
    "heading":     "'Issue 2: Bittersweet Blossoms' is OPEN",
    "description": "Submissions for our second issue, \"Bittersweet Blossoms\" are open! We accept anything creative, with a maximum of 5 submissions for each individual. Please click on the button to access our submission form! We look forward to seeing your submissions!",
    "form_url":    "https://docs.google.com/forms/d/e/1FAIpQLSe7VdYUW2mfKJSXzgqajt5Wu6sqW3-o6jVICCLX4dNeM3KNeg/viewform?usp=dialog",
    "closed_heading":     "Issue submissions are currently closed.",
    "closed_description": "We're currently paused on accepting submissions for our quarterly issues. Please do consider submitting to our lovely fresh blog in the meantime, or booking an artist interview with us if it interests you!",
}

@app.route("/pages/submissions", methods=["GET", "POST"])
@login_required
def page_submissions():
    config = load_submissions_config() or _DEFAULT_SUBMISSIONS
    if request.method == "POST":
        config = {
            "is_open":            request.form.get("is_open") == "1",
            "heading":            request.form.get("heading", "").strip(),
            "description":        request.form.get("description", "").strip(),
            "form_url":           request.form.get("form_url", "").strip(),
            "closed_heading":     request.form.get("closed_heading", "").strip(),
            "closed_description": request.form.get("closed_description", "").strip(),
        }
        save_submissions_config(config)
        flash("Submissions page saved.", "success")
        return redirect(url_for("page_submissions"))
    return render_template("page_submissions.html", config=config)

# ---------------------------------------------------------------------------
# Blog page header
# ---------------------------------------------------------------------------
_DEFAULT_BLOG_PAGE = {
    "heading":     "Blog submissions are open all year round.",
    "description": "We're actively seeking works from creators of all ages and nationalities to feature on our all-encompassing fresh blog! We welcome original Literature, Art, Photography, Experimental Work and other media types. Please submit a new form for each piece!",
    "form_url":    "https://docs.google.com/forms/d/e/1FAIpQLScEvU2fPNtD6t7sfV1XRj90IBXq-6qYQX4oqijHf0j9zEv8iw/viewform?usp=header",
}

@app.route("/pages/blog", methods=["GET", "POST"])
@login_required
def page_blog():
    config = load_blog_page_config() or _DEFAULT_BLOG_PAGE
    if request.method == "POST":
        config = {
            "heading":     request.form.get("heading", "").strip(),
            "description": request.form.get("description", "").strip(),
            "form_url":    request.form.get("form_url", "").strip(),
        }
        save_blog_page_config(config)
        flash("Blog page header saved.", "success")
        return redirect(url_for("page_blog"))
    return render_template("page_blog.html", config=config)

# ---------------------------------------------------------------------------
# Interviews page header
# ---------------------------------------------------------------------------
_DEFAULT_INTERVIEWS_PAGE = {
    "heading":     "Fresh interviews with international talents",
    "description": "We're excited to share conversations with creators in literature, art, music, and film, showcasing their unique voices and creative journeys.",
    "form_url":    "",
}

@app.route("/pages/interviews", methods=["GET", "POST"])
@login_required
def page_interviews():
    config = load_interviews_page_config() or _DEFAULT_INTERVIEWS_PAGE
    if request.method == "POST":
        config = {
            "heading":     request.form.get("heading", "").strip(),
            "description": request.form.get("description", "").strip(),
            "form_url":    request.form.get("form_url", "").strip(),
        }
        save_interviews_page_config(config)
        flash("Interviews page header saved.", "success")
        return redirect(url_for("page_interviews"))
    return render_template("page_interviews.html", config=config)


# ---------------------------------------------------------------------------
# Source viewer/editor — read or edit raw .py files directly from R2
# ---------------------------------------------------------------------------

def _source_view(folder, slug_or_filename, back_route, editable=True):
    """Shared logic: fetch R2 source, render source_editor.html."""
    all_loaders = {
        'blog': load_blog_posts_from_r2,
        'interviews': load_interviews_from_r2,
        'issues': load_issues_from_r2,
    }
    items = all_loaders[folder]()
    # Match by slug first, then by _filename
    item = next((i for i in items if i.get('slug') == slug_or_filename), None)
    if not item:
        item = next((i for i in items if i.get('_filename') == slug_or_filename), None)
    if not item:
        flash(f"Entry not found: {slug_or_filename}", "error")
        return redirect(url_for(back_route))
    filename = item.get('_filename') or (_slugify(item.get('title') or item.get('issue_name', '')) + '.py')
    key = f"{R2_PROJECT_FOLDER}/{folder}/{filename}"
    source = _fetch_source(key) or '# Source not found in R2'
    return filename, key, source, item

@app.route("/blog/<slug>/source", methods=["GET", "POST"])
@login_required
def blog_source(slug):
    result = _source_view('blog', slug, 'blog')
    if not isinstance(result, tuple):
        return result
    filename, key, source, item = result
    if request.method == "POST":
        new_source = request.form.get('source', '')
        _client().put_object(
            Bucket=R2_BUCKET_NAME, Key=key,
            Body=new_source.encode('utf-8'), ContentType='text/x-python'
        )
        flash(f"Source saved to R2: {filename}", "success")
        return redirect(url_for('blog_source', slug=slug))
    return render_template('source_editor.html',
        filename=filename,
        filepath=key,
        source=source,
        back_url=url_for('blog'),
        editable=True,
    )

@app.route("/interviews/<slug>/source", methods=["GET", "POST"])
@login_required
def interview_source(slug):
    result = _source_view('interviews', slug, 'interviews')
    if not isinstance(result, tuple):
        return result
    filename, key, source, item = result
    if request.method == "POST":
        new_source = request.form.get('source', '')
        _client().put_object(
            Bucket=R2_BUCKET_NAME, Key=key,
            Body=new_source.encode('utf-8'), ContentType='text/x-python'
        )
        flash(f"Source saved to R2: {filename}", "success")
        return redirect(url_for('interview_source', slug=slug))
    return render_template('source_editor.html',
        filename=filename,
        filepath=key,
        source=source,
        back_url=url_for('interviews'),
        editable=True,
    )

@app.route("/issues/<slug>/source", methods=["GET", "POST"])
@login_required
def issue_source(slug):
    result = _source_view('issues', slug, 'issues')
    if not isinstance(result, tuple):
        return result
    filename, key, source, item = result
    if request.method == "POST":
        new_source = request.form.get('source', '')
        _client().put_object(
            Bucket=R2_BUCKET_NAME, Key=key,
            Body=new_source.encode('utf-8'), ContentType='text/x-python'
        )
        flash(f"Source saved to R2: {filename}", "success")
        return redirect(url_for('issue_source', slug=slug))
    return render_template('source_editor.html',
        filename=filename,
        filepath=key,
        source=source,
        back_url=url_for('issues'),
        editable=True,
    )

# ---------------------------------------------------------------------------
# Form builder — edit form config (questions, open/closed, etc.)
# ---------------------------------------------------------------------------
_FORM_LABELS = {
    "issue_submission": "Issue Submission Form",
    "blog_submission":  "Blog Submission Form",
    "staff_application": "Staff Application Form",
}

@app.route("/forms/<form_type>", methods=["GET", "POST"])
@login_required
def form_builder(form_type):
    if form_type not in _FORM_LABELS:
        flash("Unknown form type.", "error")
        return redirect(url_for("dashboard"))
    config = load_form_config(form_type)
    if request.method == "POST":
        config["title"]          = request.form.get("title", "").strip()
        config["description"]    = request.form.get("description", "").strip()
        config["open"]           = request.form.get("open") == "1"
        config["closed_message"] = request.form.get("closed_message", "").strip()
        # Rebuild fields from dynamic form inputs
        fields = []
        i = 0
        while True:
            fid = request.form.get(f"field_id_{i}", "").strip()
            if not fid and request.form.get(f"field_label_{i}") is None:
                break
            if fid:
                ftype    = request.form.get(f"field_type_{i}", "text")
                flabel   = request.form.get(f"field_label_{i}", "").strip()
                freq     = request.form.get(f"field_required_{i}") == "1"
                fph      = request.form.get(f"field_placeholder_{i}", "").strip()
                faccept  = request.form.get(f"field_accept_{i}", "").strip()
                foptions_raw = request.form.get(f"field_options_{i}", "").strip()
                foptions = [o.strip() for o in foptions_raw.splitlines() if o.strip()]
                field = {"id": fid, "label": flabel, "type": ftype, "required": freq}
                if fph:      field["placeholder"] = fph
                if faccept:  field["accept"]      = faccept
                if foptions: field["options"]      = foptions
                fields.append(field)
            i += 1
        config["fields"] = fields
        save_form_config(form_type, config)
        flash(f"{_FORM_LABELS[form_type]} saved.", "success")
        return redirect(url_for("form_builder", form_type=form_type))
    return render_template("form_builder.html", form_type=form_type,
                           label=_FORM_LABELS[form_type], config=config)

# ---------------------------------------------------------------------------
# Submissions viewer
# ---------------------------------------------------------------------------
@app.route("/submissions/<form_type>")
@login_required
def submissions_list(form_type):
    if form_type not in _FORM_LABELS:
        flash("Unknown form type.", "error")
        return redirect(url_for("dashboard"))
    subs = load_submissions(form_type)
    config = load_form_config(form_type)
    return render_template("submissions_list.html", form_type=form_type,
                           label=_FORM_LABELS[form_type], submissions=subs, config=config)

@app.route("/submissions/<form_type>/<sub_id>/delete", methods=["POST"])
@login_required
def submission_delete(form_type, sub_id):
    delete_submission(form_type, sub_id)
    flash("Submission deleted.", "success")
    return redirect(url_for("submissions_list", form_type=form_type))

# Proxy for submission files (same as magazine)
@app.route("/r2-submission-file/<path:filename>")
@login_required
def r2_submission_file(filename):
    from botocore.exceptions import ClientError
    from flask import Response
    key = f"{R2_PROJECT_FOLDER}/submission-files/{filename}"
    try:
        obj = _client().get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return Response(obj["Body"].read(), content_type=obj["ContentType"])
    except ClientError:
        return ("Not found", 404)

# ---------------------------------------------------------------------------
# R2 image proxy — serves fresh-universe/images/<filename>
# ---------------------------------------------------------------------------
@app.route("/r2-image/<path:filename>")
def r2_image(filename):
    from botocore.exceptions import ClientError
    from flask import Response
    from r2_storage import _client, R2_BUCKET_NAME, R2_PROJECT_FOLDER
    key = f"{R2_PROJECT_FOLDER}/images/{filename}"
    try:
        obj = _client().get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return Response(obj["Body"].read(), content_type=obj["ContentType"])
    except ClientError:
        return ("Not found", 404)

# ---------------------------------------------------------------------------
# Image upload — stores to R2 fresh-universe/images/, returns public URL
# ---------------------------------------------------------------------------
@app.route("/upload/image", methods=["POST"])
@login_required
def upload_image():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    allowed = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
    if ext not in allowed:
        return jsonify({"error": "File type not allowed"}), 400
    # Sanitise filename
    safe_name = re.sub(r"[^\w.\-]", "_", f.filename)
    data = f.read()
    url = upload_image_to_r2(safe_name, data, f.content_type or "application/octet-stream")
    return jsonify({"url": url})


if __name__ == "__main__":
    ensure_project_folder()
    app.run(debug=True, port=5001)
