from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import os, re, hashlib, secrets, uuid as _uuid
from datetime import datetime, timedelta
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
    get_active_form_version, set_active_form_version, clear_active_form_version,
    list_form_versions, load_form_version, save_form_version, delete_form_version,
    R2_PROJECT_FOLDER, R2_BUCKET_NAME, _client,
    # new helpers
    load_users, save_users, get_user_by_id, get_user_by_email, upsert_user,
    load_user_groups, save_user_groups,
    load_invites, save_invites, get_invite_by_token,
    load_feedback, save_feedback,
    update_submission_fields, find_submission,
    ALL_PERMISSION_KEYS, EIC_PERMISSION_KEYS,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.jinja_env.filters['enumerate'] = enumerate
import json as _json
app.jinja_env.filters['from_json'] = _json.loads

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _hash(password):
    return hashlib.sha256(password.encode()).hexdigest()

def _set_session(user):
    """Populate session from a user dict."""
    session["user_id"]      = user["id"]
    session["user_email"]   = user["email"]
    session["display_name"] = user.get("display_name") or user["email"].split("@")[0]
    session["user_role"]    = user.get("role", "user")
    # Compute permissions
    groups = load_user_groups()
    session["permissions"]  = list(_compute_permissions(user, groups))
    # Legacy key kept so base.html session.user still works during transition
    session["user"]         = session["display_name"]

def _compute_permissions(user, groups):
    role = user.get("role", "user")
    if role == "admin":
        return set(ALL_PERMISSION_KEYS)
    if role == "editor_in_chief":
        return set(EIC_PERMISSION_KEYS)
    perms = set()
    if role == "editor":
        perms.update(["can_view_submissions", "can_edit_submissions"])
    user_group_ids = set(user.get("groups", []))
    for g in groups:
        if g["id"] in user_group_ids:
            perms.update(g.get("permissions", []))
    return perms

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login"))
            if session.get("user_role") not in roles:
                flash("Access denied.", "error")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return decorated
    return decorator

def permission_required(key):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login"))
            if key not in session.get("permissions", []):
                flash("Access denied.", "error")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return decorated
    return decorator

# Seed first admin from users.txt if users.json is empty (migration helper)
def _maybe_seed_admin():
    users = load_users()
    if users:
        return
    users_file = os.path.join(os.path.dirname(__file__), "users.txt")
    if not os.path.exists(users_file):
        return
    with open(users_file) as fh:
        for line in fh:
            line = line.strip()
            if ":" in line:
                uname, ph = line.split(":", 1)
                admin = {
                    "id": str(_uuid.uuid4()),
                    "email": f"{uname.strip()}@freshuniverse.local",
                    "display_name": uname.strip(),
                    "password_hash": ph.strip(),
                    "role": "admin",
                    "groups": [],
                    "created_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "invited_by": None,
                }
                upsert_user(admin)
                break  # only first user

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    _maybe_seed_admin()
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_user_by_email(email)
        if user and user.get("password_hash") == _hash(password):
            _set_session(user)
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")

@app.route("/invite/<token>", methods=["GET", "POST"])
def invite_signup(token):
    invite = get_invite_by_token(token)
    if not invite or invite.get("used") or invite.get("revoked"):
        flash("This invite link is invalid or has already been used.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        display  = request.form.get("display_name", "").strip()
        if not email or not password:
            flash("Email and password are required.", "error")
        elif get_user_by_email(email):
            flash("An account with that email already exists.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            new_user = {
                "id": str(_uuid.uuid4()),
                "email": email,
                "display_name": display or email.split("@")[0],
                "password_hash": _hash(password),
                "role": "user",
                "groups": [],
                "created_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "invited_by": invite.get("created_by"),
            }
            upsert_user(new_user)
            # Mark invite used
            invites = load_invites()
            for inv in invites:
                if inv["token"] == token:
                    inv["used"] = True
                    inv["used_by_user_id"] = new_user["id"]
            save_invites(invites)
            _set_session(new_user)
            flash("Account created! Welcome to Fresh Universe CMS.", "success")
            return redirect(url_for("dashboard"))
    return render_template("invite_signup.html", invite=invite, token=token)

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
        role_count = int(request.form.get("role_count", 0))
        for i in range(role_count):
            name = request.form.get(f"role_name_{i}", "").strip()
            desc = request.form.get(f"role_desc_{i}", "").strip()
            if name:
                is_hiring = request.form.get(f"role_hiring_{i}") == "1"
                roles.append({"name": name, "description": desc, "is_hiring": is_hiring})
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

def _parse_form_fields(request):
    """Parse dynamic field inputs from a form builder POST request."""
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
            fmultiple = request.form.get(f"field_multiple_{i}") == "1"
            foptions_raw = request.form.get(f"field_options_{i}", "").strip()
            foptions = [o.strip() for o in foptions_raw.splitlines() if o.strip()]
            fmax_count_raw = request.form.get(f"field_max_count_{i}", "").strip()
            field = {"id": fid, "label": flabel, "type": ftype, "required": freq}
            if fph:        field["placeholder"] = fph
            if faccept:    field["accept"]      = faccept
            if fmultiple:  field["multiple"]    = True
            if foptions:   field["options"]     = foptions
            if ftype == "piece_count_selector" and fmax_count_raw:
                try:
                    field["max_count"] = max(1, min(10, int(fmax_count_raw)))
                except ValueError:
                    field["max_count"] = 4
            fields.append(field)
        i += 1
    return fields

@app.route("/forms/<form_type>", methods=["GET", "POST"])
@login_required
def form_builder(form_type):
    if form_type not in _FORM_LABELS:
        flash("Unknown form type.", "error")
        return redirect(url_for("dashboard"))
    active_version = get_active_form_version(form_type)
    versions = list_form_versions(form_type)
    # Load config from active version if set, else legacy
    if active_version:
        config = load_form_version(form_type, active_version) or load_form_config(form_type)
    else:
        config = load_form_config(form_type)
    if request.method == "POST":
        config["title"]          = request.form.get("title", "").strip()
        config["description"]    = request.form.get("description", "").strip()
        config["open"]           = request.form.get("open") == "1"
        config["closed_message"] = request.form.get("closed_message", "").strip()
        config["fields"] = _parse_form_fields(request)
        if active_version:
            save_form_version(form_type, active_version, config)
        else:
            save_form_config(form_type, config)
        flash(f"{_FORM_LABELS[form_type]} saved.", "success")
        return redirect(url_for("form_builder", form_type=form_type))
    return render_template("form_builder.html", form_type=form_type,
                           label=_FORM_LABELS[form_type], config=config,
                           active_version=active_version, versions=versions)

# ---------------------------------------------------------------------------
# Form versioning — create, activate, delete versions
# ---------------------------------------------------------------------------
@app.route("/forms/<form_type>/landing")
@login_required
def form_landing(form_type):
    if form_type not in _FORM_LABELS:
        flash("Unknown form type.", "error")
        return redirect(url_for("dashboard"))
    versions = list_form_versions(form_type)
    active = get_active_form_version(form_type)
    return render_template("form_landing.html", form_type=form_type,
                           label=_FORM_LABELS[form_type], versions=versions, active=active)

# keep old /versions URL as alias for backwards compat
@app.route("/forms/<form_type>/versions")
@login_required
def form_versions(form_type):
    return redirect(url_for("form_landing", form_type=form_type))

@app.route("/forms/<form_type>/versions/new", methods=["POST"])
@login_required
def form_version_new(form_type):
    if form_type not in _FORM_LABELS:
        return redirect(url_for("dashboard"))
    version_name = re.sub(r"[^\w\-]", "_", request.form.get("version_name", "").strip())
    if not version_name:
        flash("Version name is required.", "error")
        return redirect(url_for("form_landing", form_type=form_type))
    # Copy current active config as starting point
    existing = list_form_versions(form_type)
    if version_name in existing:
        flash("A version with that name already exists.", "error")
        return redirect(url_for("form_landing", form_type=form_type))
    base_config = load_form_config(form_type)
    save_form_version(form_type, version_name, base_config)
    flash(f"Version '{version_name}' created.", "success")
    return redirect(url_for("form_landing", form_type=form_type))

@app.route("/forms/<form_type>/versions/<version_name>/activate", methods=["POST"])
@login_required
def form_version_activate(form_type, version_name):
    if form_type not in _FORM_LABELS:
        return redirect(url_for("dashboard"))
    set_active_form_version(form_type, version_name)
    flash(f"Version '{version_name}' is now active.", "success")
    return redirect(url_for("form_landing", form_type=form_type))

@app.route("/forms/<form_type>/versions/deactivate", methods=["POST"])
@login_required
def form_version_deactivate(form_type):
    if form_type not in _FORM_LABELS:
        return redirect(url_for("dashboard"))
    clear_active_form_version(form_type)
    flash("Active version cleared — using default form.", "success")
    return redirect(url_for("form_landing", form_type=form_type))

@app.route("/forms/<form_type>/versions/<version_name>/edit", methods=["GET", "POST"])
@login_required
def form_version_edit(form_type, version_name):
    if form_type not in _FORM_LABELS:
        flash("Unknown form type.", "error")
        return redirect(url_for("dashboard"))
    config = load_form_version(form_type, version_name) or load_form_config(form_type)
    active_version = get_active_form_version(form_type)
    if request.method == "POST":
        config["title"]          = request.form.get("title", "").strip()
        config["description"]    = request.form.get("description", "").strip()
        config["open"]           = request.form.get("open") == "1"
        config["closed_message"] = request.form.get("closed_message", "").strip()
        config["fields"] = _parse_form_fields(request)
        save_form_version(form_type, version_name, config)
        flash(f"Version '{version_name}' saved.", "success")
        return redirect(url_for("form_version_edit", form_type=form_type, version_name=version_name))
    return render_template("form_builder.html", form_type=form_type,
                           label=f"{_FORM_LABELS[form_type]} — {version_name}",
                           config=config, active_version=active_version,
                           versions=list_form_versions(form_type),
                           editing_version=version_name,
                           back_url=url_for("form_landing", form_type=form_type))

@app.route("/forms/<form_type>/versions/<version_name>/delete", methods=["POST"])
@login_required
def form_version_delete(form_type, version_name):
    if form_type not in _FORM_LABELS:
        return redirect(url_for("dashboard"))
    delete_form_version(form_type, version_name)
    flash(f"Version '{version_name}' deleted.", "success")
    return redirect(url_for("form_landing", form_type=form_type))

# ---------------------------------------------------------------------------
# Submissions viewer
# ---------------------------------------------------------------------------
@app.route("/submissions/<form_type>")
@login_required
def submissions_list(form_type):
    if form_type not in _FORM_LABELS:
        flash("Unknown form type.", "error")
        return redirect(url_for("dashboard"))
    version_filter = request.args.get("version")
    subs = load_submissions(form_type, version_name=version_filter if version_filter else None)
    versions = list_form_versions(form_type)
    # Config: use version-specific config for field labels if viewing a specific version
    if version_filter:
        config = load_form_version(form_type, version_filter) or load_form_config(form_type)
    else:
        config = load_form_config(form_type)
    return render_template("submissions_list.html", form_type=form_type,
                           label=_FORM_LABELS[form_type], submissions=subs, config=config,
                           version_list=versions, version_filter=version_filter,
                           all_editors=_get_editors())

@app.route("/submissions/<form_type>/v/<version_name>")
@login_required
def submissions_version(form_type, version_name):
    if form_type not in _FORM_LABELS:
        flash("Unknown form type.", "error")
        return redirect(url_for("dashboard"))
    subs = load_submissions(form_type, version_name=version_name)
    config = load_form_version(form_type, version_name) or load_form_config(form_type)
    active = get_active_form_version(form_type)
    return render_template("submissions_list.html", form_type=form_type,
                           label=_FORM_LABELS[form_type], submissions=subs, config=config,
                           version_list=list_form_versions(form_type),
                           version_filter=version_name,
                           back_url=url_for("form_landing", form_type=form_type),
                           editing_version=version_name,
                           active_version=active,
                           all_editors=_get_editors())

@app.route("/submissions/<form_type>/<sub_id>/delete", methods=["POST"])
@login_required
def submission_delete(form_type, sub_id):
    version = request.form.get("version")
    delete_submission(form_type, sub_id)
    flash("Submission deleted.", "success")
    if version:
        return redirect(url_for("submissions_version", form_type=form_type, version_name=version))
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


# ---------------------------------------------------------------------------
# Admin — User management
# ---------------------------------------------------------------------------
@app.route("/admin/users")
@role_required("admin", "editor_in_chief")
def admin_users():
    users   = load_users()
    invites = [i for i in load_invites() if not i.get("used") and not i.get("revoked")]
    groups  = load_user_groups()
    return render_template("admin_users.html", users=users, pending_invites=invites, groups=groups)

@app.route("/admin/users/<user_id>/role", methods=["POST"])
@role_required("admin")
def admin_set_role(user_id):
    user = get_user_by_id(user_id)
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("admin_users"))
    new_role = request.form.get("role", "user")
    if new_role not in ("admin", "editor_in_chief", "editor", "user"):
        flash("Invalid role.", "error")
        return redirect(url_for("admin_users"))
    user["role"] = new_role
    upsert_user(user)
    flash(f"Role updated for {user['email']}.", "success")
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<user_id>/groups", methods=["POST"])
@role_required("admin")
def admin_set_user_groups(user_id):
    user = get_user_by_id(user_id)
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("admin_users"))
    user["groups"] = request.form.getlist("groups")
    upsert_user(user)
    flash(f"Groups updated for {user['email']}.", "success")
    return redirect(url_for("admin_users"))

@app.route("/admin/invites/new", methods=["POST"])
@role_required("admin")
def admin_create_invite():
    email = request.form.get("email", "").strip().lower()
    token = str(_uuid.uuid4())
    invites = load_invites()
    invites.append({
        "token": token,
        "email": email,
        "created_by": session["user_id"],
        "created_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "used": False,
        "revoked": False,
        "used_by_user_id": None,
    })
    save_invites(invites)
    invite_url = url_for("invite_signup", token=token, _external=True)
    flash(f"Invite link created: {invite_url}", "success")
    return redirect(url_for("admin_users"))

@app.route("/admin/invites/<token>/revoke", methods=["POST"])
@role_required("admin")
def admin_revoke_invite(token):
    invites = load_invites()
    for inv in invites:
        if inv["token"] == token:
            inv["revoked"] = True
    save_invites(invites)
    flash("Invite revoked.", "success")
    return redirect(url_for("admin_users"))

# ---------------------------------------------------------------------------
# Admin — Group management
# ---------------------------------------------------------------------------
@app.route("/admin/groups")
@role_required("admin")
def admin_groups():
    groups = load_user_groups()
    return render_template("admin_groups.html", groups=groups, all_permissions=ALL_PERMISSION_KEYS)

@app.route("/admin/groups/new", methods=["GET", "POST"])
@role_required("admin")
def admin_group_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Group name is required.", "error")
            return render_template("admin_group_form.html", mode="new", group={}, all_permissions=ALL_PERMISSION_KEYS)
        perms = request.form.getlist("permissions")
        group = {"id": str(_uuid.uuid4()), "name": name, "permissions": perms}
        groups = load_user_groups()
        groups.append(group)
        save_user_groups(groups)
        flash(f"Group '{name}' created.", "success")
        return redirect(url_for("admin_groups"))
    return render_template("admin_group_form.html", mode="new", group={}, all_permissions=ALL_PERMISSION_KEYS)

@app.route("/admin/groups/<group_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def admin_group_edit(group_id):
    groups = load_user_groups()
    group  = next((g for g in groups if g["id"] == group_id), None)
    if not group:
        flash("Group not found.", "error")
        return redirect(url_for("admin_groups"))
    if request.method == "POST":
        group["name"]        = request.form.get("name", "").strip()
        group["permissions"] = request.form.getlist("permissions")
        save_user_groups(groups)
        flash(f"Group '{group['name']}' updated.", "success")
        return redirect(url_for("admin_groups"))
    return render_template("admin_group_form.html", mode="edit", group=group, all_permissions=ALL_PERMISSION_KEYS)

@app.route("/admin/groups/<group_id>/delete", methods=["POST"])
@role_required("admin")
def admin_group_delete(group_id):
    groups = [g for g in load_user_groups() if g["id"] != group_id]
    save_user_groups(groups)
    flash("Group deleted.", "success")
    return redirect(url_for("admin_groups"))

# ---------------------------------------------------------------------------
# Editorial workflow — assign submissions
# ---------------------------------------------------------------------------
@app.route("/submissions/<form_type>/<sub_id>/assign", methods=["POST"])
@login_required
def submission_assign(form_type, sub_id):
    if session.get("user_role") not in ("admin", "editor_in_chief"):
        flash("Access denied.", "error")
        return redirect(url_for("submissions_list", form_type=form_type))
    editor_id  = request.form.get("editor_id", "").strip()
    days_raw   = request.form.get("deadline_days", "7").strip()
    try:
        days = max(1, int(days_raw))
    except ValueError:
        days = 7
    deadline = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = {
        "_assigned_to":      editor_id or None,
        "_assigned_by":      session["user_id"],
        "_assigned_at":      datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_deadline_days":    days,
        "_deadline_date":    deadline,
        "_editorial_status": "pending" if editor_id else "unassigned",
    }
    update_submission_fields(form_type, sub_id, fields)
    flash("Submission assigned.", "success")
    return redirect(url_for("submissions_list", form_type=form_type))

# ---------------------------------------------------------------------------
# Editorial workflow — editor dashboard
# ---------------------------------------------------------------------------
@app.route("/editor/dashboard")
@login_required
def editor_dashboard():
    if session.get("user_role") not in ("admin", "editor_in_chief", "editor"):
        flash("Access denied.", "error")
        return redirect(url_for("dashboard"))
    uid = session["user_id"]
    all_subs = []
    for ft in ("blog_submission", "issue_submission", "staff_application"):
        for s in load_submissions(ft):
            s["_form_type_label"] = _FORM_LABELS.get(ft, ft)
            all_subs.append(s)
    # Filter to current user's assignments (editors see only theirs; admin/EIC see all)
    if session["user_role"] == "editor":
        my_subs = [s for s in all_subs if s.get("_assigned_to") == uid]
    else:
        my_subs = [s for s in all_subs if s.get("_assigned_to")]
    pending   = [s for s in my_subs if s.get("_editorial_status") in ("pending", "in_review")]
    completed = [s for s in my_subs if s.get("_editorial_status") == "completed"]
    # Deadline countdown helper
    now = datetime.utcnow()
    for s in pending:
        dl = s.get("_deadline_date")
        if dl:
            try:
                delta = datetime.strptime(dl, "%Y-%m-%dT%H:%M:%SZ") - now
                s["_days_left"] = max(0, delta.days)
            except Exception:
                s["_days_left"] = None
    return render_template("editor_dashboard.html",
        pending=pending, completed=completed,
        all_editors=_get_editors(),
    )

def _get_editors():
    return [u for u in load_users() if u.get("role") in ("editor", "editor_in_chief", "admin")]

# ---------------------------------------------------------------------------
# Editorial workflow — editor view (split: files + feedback form)
# ---------------------------------------------------------------------------
@app.route("/editor/submission/<form_type>/<sub_id>", methods=["GET"])
@login_required
def editor_view(form_type, sub_id):
    sub = find_submission(form_type, sub_id)
    if not sub:
        flash("Submission not found.", "error")
        return redirect(url_for("editor_dashboard"))
    # Only assigned editor, EIC, or admin can view
    uid = session["user_id"]
    role = session.get("user_role")
    if role not in ("admin", "editor_in_chief") and sub.get("_assigned_to") != uid:
        flash("Access denied.", "error")
        return redirect(url_for("editor_dashboard"))
    feedback = load_feedback(sub_id)
    config   = load_form_config(form_type)
    assigned_editor = get_user_by_id(sub.get("_assigned_to")) if sub.get("_assigned_to") else None
    return render_template("editor_view.html",
        sub=sub, form_type=form_type, config=config,
        feedback=feedback, assigned_editor=assigned_editor,
    )

@app.route("/editor/submission/<form_type>/<sub_id>/feedback", methods=["POST"])
@login_required
def editor_submit_feedback(form_type, sub_id):
    sub = find_submission(form_type, sub_id)
    if not sub:
        flash("Submission not found.", "error")
        return redirect(url_for("editor_dashboard"))
    uid  = session["user_id"]
    role = session.get("user_role")
    if role not in ("admin", "editor_in_chief") and sub.get("_assigned_to") != uid:
        flash("Access denied.", "error")
        return redirect(url_for("editor_dashboard"))
    decision   = request.form.get("decision", "")
    feedback_text   = request.form.get("feedback_text", "").strip()
    suggestion_text = request.form.get("suggestion_text", "").strip()
    fb_data = {
        "submission_id":   sub_id,
        "submission_type": form_type,
        "editor_id":       uid,
        "feedback_text":   feedback_text,
        "suggestion_text": suggestion_text,
        "decision":        decision,
        "submitted_at":    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_feedback(sub_id, fb_data)
    status = "completed" if decision in ("accept", "reject") else "in_review"
    update_submission_fields(form_type, sub_id, {"_editorial_status": status})
    flash("Feedback submitted.", "success")
    return redirect(url_for("editor_view", form_type=form_type, sub_id=sub_id))

# ---------------------------------------------------------------------------
# Editor feedback view (EIC/admin reads feedback alongside piece)
# ---------------------------------------------------------------------------
@app.route("/editor/submission/<form_type>/<sub_id>/review")
@login_required
def editor_review(form_type, sub_id):
    if session.get("user_role") not in ("admin", "editor_in_chief"):
        flash("Access denied.", "error")
        return redirect(url_for("dashboard"))
    sub      = find_submission(form_type, sub_id)
    feedback = load_feedback(sub_id)
    config   = load_form_config(form_type)
    editor   = get_user_by_id(sub.get("_assigned_to")) if sub and sub.get("_assigned_to") else None
    return render_template("editor_review.html",
        sub=sub, form_type=form_type, config=config,
        feedback=feedback, editor=editor,
    )


if __name__ == "__main__":
    ensure_project_folder()
    app.run(debug=True, port=5001)
