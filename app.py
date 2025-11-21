import os
import json
import re
import requests
import time
from functools import wraps
from io import BytesIO

from flask import Flask, request, jsonify, render_template, send_file, make_response
from werkzeug.utils import secure_filename

# --- Local parsing libs ---
try:
    import docx
    from docx.shared import Pt
    from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
    from pypdf import PdfReader
except ImportError:
    print("WARNING: install parsing libs: pip install python-docx pypdf")
    docx = None
    PdfReader = None

# --- PDF generation ---
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
except ImportError:
    print("WARNING: install reportlab: pip install reportlab")

# --- OpenRouter (OpenAI client) ---
try:
    from openai import OpenAI, APIError as OpenAIAPIError
except Exception:
    print("WARNING: OpenAI client not installed. pip install openai")
    OpenAI = None
    OpenAIAPIError = Exception

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# ====== CONFIGURE YOUR KEYS HERE ======
OPENAI_API_KEY = ""  # <- put real key
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent"

openai_client = None
try:
    if OPENAI_API_KEY and OPENAI_API_KEY != "YOUR_OPENROUTER_API_KEY_HERE" and OpenAI:
        openai_client = OpenAI(
            api_key=OPENAI_API_KEY,
            base_url="https://openrouter.ai/api/v1"
        )
except Exception as e:
    print(f"OpenAI client init warning: {e}")


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ===================== LaTeX TEMPLATES =====================

LATEX_TEMPLATE_BASIC = r"""
\documentclass[10pt, a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{geometry}
\geometry{a4paper, top=1.5cm, bottom=1.5cm, left=1.5cm, right=1.5cm}
\usepackage{titlesec}
\usepackage{hyperref}
\hypersetup{colorlinks=true, urlcolor=blue}
\usepackage{enumitem}

\pagestyle{empty}
\setlength{\parindent}{0pt}
\titleformat{\section}{\large\bfseries}{}{0em}{}[\color{black}\titlerule]

\begin{document}
\vspace*{0.2cm}
\begin{center}
    \textbf{\Large NAME\_PLACEHOLDER}
    
    \vspace{0.1cm}
    \small \href{mailto:EMAIL\_PLACEHOLDER}{EMAIL\_PLACEHOLDER} $|$ PHONE\_PLACEHOLDER $|$ \href{LINKEDIN\_PLACEHOLDER}{LinkedIn}
    \vspace{0.1cm}
\end{center}

\section*{Experience}
EXPERIENCE\_PLACEHOLDER

\section*{Education}
EDUCATION\_PLACEHOLDER

\section*{Skills}
SKILLS\_PLACEHOLDER

\end{document}
"""

LATEX_TEMPLATE_AUTOCV = r"""
\documentclass[11pt, a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{geometry}
\geometry{a4paper, top=1cm, bottom=1cm, left=2cm, right=2cm}
\usepackage{titlesec}
\usepackage{hyperref}
\hypersetup{colorlinks=true, urlcolor=black, linkcolor=black}
\usepackage{enumitem}
\usepackage{fontawesome5}

\pagestyle{empty}
\setlength{\parindent}{0pt}
\titleformat{\section}{\Large\bfseries\sffamily\color{black!80}}{}{0em}{}[\color{black}\titlerule]

\begin{document}

\begin{center}
    {\Huge\bfseries NAME\_PLACEHOLDER}
    
    \vspace{0.1cm}
    \faIcon{envelope} \href{mailto:EMAIL\_PLACEHOLDER}{EMAIL\_PLACEHOLDER} $\bullet$ \faIcon{phone} PHONE\_PLACEHOLDER $\bullet$ \faIcon{linkedin} \href{LINKEDIN\_PLACEHOLDER}{LinkedIn}
    \vspace{0.2cm}
\end{center}

\section{Experience}
EXPERIENCE\_PLACEHOLDER

\section{Education}
EDUCATION\_PLACEHOLDER

\section{Skills}
SKILLS\_PLACEHOLDER

\end{document}
"""


def get_template_by_name(template_name: str) -> str:
    templates = {
        "Modern ATS (Basic)": LATEX_TEMPLATE_BASIC,
        "Professional AutoCV": LATEX_TEMPLATE_AUTOCV,
    }
    return templates.get(template_name, LATEX_TEMPLATE_BASIC)


def escape_latex(text: str) -> str:
    text = str(text)
    text = text.replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_')
    text = text.replace('{', '\\{').replace('}', '\\}')
    return text


def format_for_latex(data, format_type: str) -> str:
    if format_type == 'experience' and isinstance(data, list):
        out = []
        for job in data:
            if isinstance(job, dict):
                title = escape_latex(job.get('title', 'Job Title'))
                company = escape_latex(job.get('company', 'Company'))
                dates = escape_latex(job.get('dates', 'Dates'))
                desc = job.get('description', [])
                out.append(f"\\textbf{{{title}}} | {company} \\hfill \\textit{{{dates}}}")
                out.append("\\begin{itemize}[leftmargin=*, noitemsep, topsep=1pt, parsep=0pt]")
                if isinstance(desc, list):
                    for b in desc:
                        out.append(f"    \\item {escape_latex(b)}")
                out.append("\\end{itemize}\n")
            elif isinstance(job, str):
                out.append(escape_latex(job) + "\n")
        return "\n".join(out)

    if format_type == 'education' and isinstance(data, list):
        out = []
        for edu in data:
            if isinstance(edu, dict):
                degree = escape_latex(edu.get('degree', 'Degree'))
                inst = escape_latex(edu.get('institution', 'Institution'))
                dates = escape_latex(edu.get('dates', 'Dates'))
                out.append(f"\\textbf{{{degree}}} | {inst} \\hfill \\textit{{{dates}}}")
                if 'description' in edu:
                    out.append(f"\n\\textit{{{escape_latex(edu['description'])}}}\n")
            elif isinstance(edu, str):
                out.append(escape_latex(edu) + "\n")
        return "\n".join(out)

    if format_type == 'skills' and isinstance(data, list):
        return ", ".join(escape_latex(s) for s in data)

    return str(data)


def retry_api_call(max_retries=3, initial_delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (OpenAIAPIError, requests.exceptions.RequestException) as e:
                    status_code = getattr(e, 'status_code', None)
                    if not status_code and isinstance(e, requests.exceptions.HTTPError):
                        status_code = e.response.status_code
                    if status_code and 500 <= status_code <= 599 or not status_code:
                        if attempt < max_retries - 1:
                            print(f"API error, retrying in {delay}s... ({attempt + 1}/{max_retries})")
                            time.sleep(delay)
                            delay *= 2
                        else:
                            raise
                    else:
                        raise
        return wrapper
    return decorator


# ===================== PARSING =====================

def extract_text_from_docx(filepath):
    if docx is None:
        return None
    try:
        d = docx.Document(filepath)
        return "\n".join(p.text for p in d.paragraphs)
    except Exception:
        return None


def extract_text_from_pdf(filepath):
    if PdfReader is None:
        return None
    try:
        reader = PdfReader(filepath)
        text = ""
        for page in reader.pages:
            t = page.extract_text() or ""
            text += t
        return text
    except Exception:
        return None


@retry_api_call(max_retries=3)
def call_openai_structuring(raw_text: str) -> str:
    """Use OpenRouter model to parse raw resume text into structured JSON."""
    prompt = (
        "You are an expert resume parser. Analyze the following raw resume text and return "
        "a single JSON object with keys: 'name', 'email', 'phone', 'linkedin', 'education' "
        "(list of objects with 'degree','institution','dates','description'), "
        "'experience' (list of objects with 'title','company','dates','description' = list of bullets), "
        "and 'skills' (list of strings). "
        "Return ONLY the JSON object.\n"
        f"RAW:\n{raw_text}"
    )
    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a strict JSON-only resume parser."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"}
    )
    return resp.choices[0].message.content


def parse_resume_content(content, source_type='file', filepath=None):
    if not openai_client:
        return {"error": "AI client not configured (OpenRouter key missing)."}

    raw_text = None
    if source_type == 'file' and filepath:
        ext = filepath.rsplit('.', 1)[1].lower()
        if ext == 'pdf':
            raw_text = extract_text_from_pdf(filepath)
        elif ext == 'docx':
            raw_text = extract_text_from_docx(filepath)
        elif ext == 'txt':
            with open(filepath, 'r', encoding='utf-8') as f:
                raw_text = f.read()
    elif source_type == 'manual':
        return content

    if not raw_text:
        return {"error": "Could not extract text from file."}

    try:
        json_text = call_openai_structuring(raw_text)
        structured = json.loads(json_text)
        return {"structured_data": structured}
    except Exception as e:
        return {"error": f"LLM structuring failed: {e}"}


# ===================== ATS SCORE =====================

ACTION_VERBS = [
    'led', 'developed', 'designed', 'implemented', 'created', 'improved', 'managed', 'built', 'reduced',
    'increased', 'decreased', 'optimized', 'automated', 'delivered', 'launched', 'achieved', 'supported'
]
NUMBER_REGEX = re.compile(r"\b\d+[\d,\.]*%?\b")


def extract_text_from_structured(structured):
    parts = []
    if isinstance(structured, dict):
        for key in ['name', 'email', 'phone', 'linkedin', 'experience', 'education', 'skills', 'summary']:
            val = structured.get(key)
            if not val:
                continue
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        for v in item.values():
                            if isinstance(v, list):
                                parts.extend(str(x) for x in v)
                            else:
                                parts.append(str(v))
                    else:
                        parts.append(str(item))
            else:
                parts.append(str(val))
    return "\n".join(parts)


def compute_ats_score(structured_data, job_description=""):
    text = extract_text_from_structured(structured_data).lower()
    jd = (job_description or "").lower()
    feedback = []

    # completeness (30)
    completeness_points = 0
    required_sections = ['experience', 'education', 'skills']
    for sec in required_sections:
        if structured_data.get(sec):
            completeness_points += 1
        else:
            feedback.append(f"Add or strengthen the {sec} section.")

    completeness_score = (completeness_points / len(required_sections)) * 30

    # keyword match (30)
    jd_terms = [t for t in re.findall(r"\w+", jd) if len(t) > 2]
    unique_jd = set(jd_terms)
    if unique_jd:
        matches = sum(1 for term in unique_jd if term in text)
        keyword_score = min(30, int((matches / max(1, len(unique_jd))) * 30))
        if matches == 0:
            feedback.append("Include more keywords from the job description in your bullets and skills.")
    else:
        keyword_score = 15

    # quantified metrics (20)
    numbers = NUMBER_REGEX.findall(text)
    quant_score = min(20, 5 * len(numbers))
    if len(numbers) == 0:
        feedback.append("Add metrics/numbers (%, counts, time saved, etc.) to show impact in your experience bullets.")

    # action verbs (10)
    verbs_found = sum(1 for v in ACTION_VERBS if v in text)
    verb_score = min(10, 2 * verbs_found)
    if verbs_found < 5:
        feedback.append("Start more bullets with strong action verbs like 'led', 'developed', 'implemented'.")

    # contact info (10)
    contact_present = structured_data.get('email') or structured_data.get('phone') or structured_data.get('linkedin')
    contact_score = 10 if contact_present else 0
    if not contact_present:
        feedback.append("Make sure email, phone and LinkedIn are clearly visible in the header.")

    raw_score = completeness_score + keyword_score + quant_score + verb_score + contact_score
    score = max(0, min(100, int(raw_score)))

    if score >= 80:
        feedback.insert(0, "Strong resume — mostly ATS-ready; only fine-tuning is needed.")
    elif score >= 60:
        feedback.insert(0, "Decent resume; improve keyword density, quantified impact, and clarity to raise ATS score.")
    else:
        feedback.insert(0, "ATS score is low; rework structure, keywords, and metrics for better screening results.")

    breakdown = {
        "completeness_score": int(completeness_score),
        "keyword_score": int(keyword_score),
        "quant_score": int(quant_score),
        "verb_score": int(verb_score),
        "contact_score": int(contact_score),
    }

    return {
        "score": score,
        "breakdown": breakdown,
        "feedback": feedback,
        "extracted_text_snippet": (text[:800] + "...") if len(text) > 800 else text,
    }


# ===================== LLM ENHANCEMENT + SUGGESTIONS =====================

@retry_api_call(max_retries=3)
def call_openai_enhancement(structured_data, job_description, ats_feedback) -> str:
    """
    Ask OpenRouter to:
      - optimize experience + skills
      - return explicit improvement_suggestions[]
    """
    original_json = json.dumps(structured_data, indent=2)
    feedback_text = "; ".join(ats_feedback or [])

    user_prompt = (
        "You are an ATS resume optimization expert.\n\n"
        "Given this parsed resume JSON and target job description, do three things:\n"
        "1. Rewrite ONLY 'experience' to:\n"
        "   - Use strong action verbs\n"
        "   - Include quantified impact (numbers/%, time saved, counts)\n"
        "   - Add or adjust keywords relevant to the job\n"
        "2. Rewrite ONLY 'skills' to group and align with the job description.\n"
        "3. Produce an 'improvement_suggestions' array (5–10 items), where each item is a short, direct suggestion:\n"
        "   - Explain WHAT to change (e.g., 'Add 2 quantified bullets under XYZ Internship').\n"
        "   - Optionally mention WHICH keywords to add.\n\n"
        "Respond ONLY with a JSON object having these keys:\n"
        "  'experience': [...],\n"
        "  'skills': [...],\n"
        "  'improvement_suggestions': [\"...\"]\n\n"
        f"Target job description:\n{job_description}\n\n"
        f"Existing ATS feedback:\n{feedback_text}\n\n"
        f"Original structured JSON:\n{original_json}"
    )

    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are an ATS resume optimizer. Output must be valid JSON object."},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"}
    )
    return resp.choices[0].message.content


def build_fallback_suggestions(initial_ats, final_ats):
    """If LLM suggestions fail, give basic heuristic ones."""
    notes = []
    if initial_ats and final_ats and final_ats["score"] != initial_ats["score"]:
        notes.append(
            f"Overall ATS score changed from {initial_ats['score']} to {final_ats['score']}. Focus on the sub-scores below."
        )

    ib = initial_ats.get("breakdown", {}) if initial_ats else {}
    fb = final_ats.get("breakdown", {}) if final_ats else {}

    mapping = {
        "keyword_score": "Increase job-specific keywords in both experience bullets and skills section.",
        "quant_score": "Add more metrics to your bullets (%, time saved, revenue, number of users, etc.).",
        "verb_score": "Start bullets with strong action verbs like 'led', 'built', 'optimized', 'implemented'.",
        "completeness_score": "Ensure experience, education, and skills sections are filled with enough detail.",
        "contact_score": "Make sure your header has clear email, phone, and LinkedIn.",
    }

    for key, msg in mapping.items():
        if key in ib and key in fb and fb[key] <= ib[key]:
            # if didn't improve, still suggest
            notes.append(msg)

    if not notes and initial_ats:
        notes.extend(initial_ats.get("feedback", []))

    return notes


def enhance_content_with_ai(structured_data, ats_feedback, job_description):
    """Use OpenRouter to optimize content + detailed suggestions."""
    if not openai_client:
        return {"error": "OpenRouter not configured.", "enhanced_data": structured_data, "improvement_suggestions": []}

    try:
        json_text = call_openai_enhancement(structured_data, job_description, ats_feedback)
        obj = json.loads(json_text)

        enhanced = structured_data.copy()
        if "experience" in obj:
            enhanced["experience"] = obj["experience"]
        if "skills" in obj:
            enhanced["skills"] = obj["skills"]

        improvement_suggestions = obj.get("improvement_suggestions", [])
        if not isinstance(improvement_suggestions, list):
            improvement_suggestions = []

        return {
            "enhanced_data": enhanced,
            "improvement_suggestions": improvement_suggestions,
        }
    except Exception as e:
        return {
            "error": f"Enhancement failed: {e}",
            "enhanced_data": structured_data,
            "improvement_suggestions": [],
        }


# ===================== LATEX / MARKDOWN / PDF HELPERS =====================

def generate_latex_source(structured_data, template_name):
    tpl = get_template_by_name(template_name)

    final_latex = tpl.replace(
        "NAME\\_PLACEHOLDER", escape_latex(structured_data.get("name", "Your Name Here"))
    ).replace(
        "EMAIL\\_PLACEHOLDER", structured_data.get("email", "your.email@example.com")
    ).replace(
        "PHONE\\_PLACEHOLDER", escape_latex(structured_data.get("phone", "Phone Number"))
    ).replace(
        "LINKEDIN\\_PLACEHOLDER", structured_data.get("linkedin", "https://linkedin.com/in/you")
    )

    final_latex = final_latex.replace(
        "EXPERIENCE\\_PLACEHOLDER", format_for_latex(structured_data.get("experience", []), "experience")
    ).replace(
        "EDUCATION\\_PLACEHOLDER", format_for_latex(structured_data.get("education", []), "education")
    ).replace(
        "SKILLS\\_PLACEHOLDER", format_for_latex(structured_data.get("skills", []), "skills")
    )

    return final_latex


def generate_docx_file(structured_data):
    if docx is None:
        return None

    document = docx.Document()
    style = document.styles["Normal"]
    font = style.font
    font.name = "Calibri"
    font.size = Pt(11)

    name = structured_data.get("name", "Your Name")
    h1 = document.add_heading(name, 0)
    h1.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER

    contact = []
    if structured_data.get("email"):
        contact.append(structured_data["email"])
    if structured_data.get("phone"):
        contact.append(structured_data["phone"])
    if structured_data.get("linkedin"):
        contact.append(structured_data["linkedin"])

    p = document.add_paragraph(" | ".join(contact))
    p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    document.add_paragraph()

    # Experience
    if structured_data.get("experience"):
        document.add_heading("Experience", level=1)
        for job in structured_data["experience"]:
            if isinstance(job, dict):
                p = document.add_paragraph()
                r = p.add_run(f"{job.get('title', 'Title')} at {job.get('company', 'Company')}")
                r.bold = True
                p.add_run(f"\n{job.get('dates', 'Dates')}")
                desc = job.get("description", [])
                if isinstance(desc, list):
                    for bullet in desc:
                        document.add_paragraph(bullet, style="List Bullet")
                document.add_paragraph()

    # Education
    if structured_data.get("education"):
        document.add_heading("Education", level=1)
        for edu in structured_data["education"]:
            if isinstance(edu, dict):
                p = document.add_paragraph()
                r = p.add_run(f"{edu.get('degree', 'Degree')} - {edu.get('institution', 'Institution')}")
                r.bold = True
                p.add_run(f"\n{edu.get('dates', 'Dates')}")
                if "description" in edu:
                    document.add_paragraph(edu["description"])
                document.add_paragraph()

    # Skills
    if structured_data.get("skills"):
        document.add_heading("Skills", level=1)
        skills = structured_data["skills"]
        if isinstance(skills, list):
            document.add_paragraph(", ".join(skills))
        else:
            document.add_paragraph(str(skills))

    buf = BytesIO()
    document.save(buf)
    buf.seek(0)
    return buf


# ===================== FLASK ROUTES =====================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process_resume():
    job_description = ""
    skip_ai = False
    structured_data = None

    content_type = request.headers.get("Content-Type", "")
    is_file_upload = content_type.startswith("multipart/form-data")

    if is_file_upload:
        job_description = request.form.get("job_description", "")
        skip_ai = request.form.get("skip_ai") in ["on", "true"]

        file = request.files.get("file")
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            file.save(filepath)

            parsing_result = parse_resume_content(None, source_type="file", filepath=filepath)
            try:
                os.remove(filepath)
            except Exception:
                pass

            if "error" in parsing_result:
                return jsonify(parsing_result), 500

            structured_data = parsing_result.get("structured_data", {})
        else:
            return jsonify({"error": "File input mode requires a valid PDF/DOCX/TXT file."}), 400
    else:
        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({"error": "Invalid request: expected file upload or JSON body."}), 400

        job_description = payload.get("job_description", "")
        skip_ai = payload.get("skip_ai", False)
        manual_data = payload.get("manual_data", {})

        if manual_data and manual_data.get("name"):
            structured_data = {
                "name": manual_data.get("name"),
                "email": manual_data.get("email"),
                "phone": manual_data.get("phone"),
                "linkedin": manual_data.get("linkedin"),
                "summary": manual_data.get("summary", ""),
                "education": manual_data.get("education", []),
                "experience": manual_data.get("experience", []),
                "skills": manual_data.get("skills", []),
                "achievements": manual_data.get("achievements", []),
                "projects": manual_data.get("projects", []),
                "certifications": manual_data.get("certifications", []),
            }

    if not structured_data:
        return jsonify({"error": "No valid resume data found."}), 400

    if not job_description:
        job_description = "General Industry Role"

    initial_ats = compute_ats_score(structured_data, job_description)
    response_payload = {
        "status": "success",
        "job_description": job_description,
        "parsed_data": structured_data,
        "initial_ats": initial_ats,
    }

    # AI enhancement + suggestions
    if not skip_ai:
        enh = enhance_content_with_ai(structured_data, initial_ats.get("feedback", []), job_description)
        enhanced_data = enh.get("enhanced_data", structured_data)
        final_ats = compute_ats_score(enhanced_data, job_description)

        # if LLM suggestions missing, build fallback
        suggestions = enh.get("improvement_suggestions") or build_fallback_suggestions(initial_ats, final_ats)

        response_payload["enhanced_data"] = enhanced_data
        response_payload["final_ats"] = final_ats
        response_payload["message"] = "Resume processed and AI-optimized."
        response_payload["improvement_suggestions"] = suggestions

        if "error" in enh:
            response_payload["ai_log"] = enh["error"]
    else:
        response_payload["enhanced_data"] = structured_data
        response_payload["final_ats"] = initial_ats
        response_payload["message"] = "Resume processed. AI enhancement skipped."
        response_payload["improvement_suggestions"] = initial_ats.get("feedback", [])

    return jsonify(response_payload)


@app.route("/generate_resume_text", methods=["POST"])
def generate_resume_text():
    data = request.get_json()
    structured_data = data.get("structured_data")
    template_name = data.get("template_name", "Modern ATS (Basic)")

    if not structured_data:
        return jsonify({"error": "No structured data provided."}), 400

    latex_src = generate_latex_source(structured_data, template_name)
    resp = make_response(latex_src)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    return resp


@app.route("/generate_resume", methods=["POST"])
def generate_resume():
    data = request.get_json()
    structured_data = data.get("structured_data")
    template_name = data.get("template_name", "Modern ATS (Basic)")

    if not structured_data:
        return jsonify({"error": "No structured data provided."}), 400

    latex_src = generate_latex_source(structured_data, template_name)
    buf = BytesIO(latex_src.encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="ats_optimized_resume.tex",
        mimetype="application/x-tex",
    )


@app.route("/generate_markdown", methods=["POST"])
def generate_markdown():
    data = request.get_json()
    structured_data = data.get("structured_data")

    if not structured_data:
        return jsonify({"error": "No structured data provided."}), 400

    md = f"# {structured_data.get('name', 'Your Name Here')}\n\n"
    md += f"**Contact:** {structured_data.get('email', 'N/A')} | {structured_data.get('phone', 'N/A')} | [LinkedIn]({structured_data.get('linkedin', '#')})\n\n"
    md += "---\n\n"

    # Experience
    md += "## Experience\n\n"
    exp = structured_data.get("experience", [])
    for job in exp:
        if isinstance(job, dict):
            md += f"### {job.get('title', 'Job Title')} @ {job.get('company', 'Company')}\n"
            md += f"*{job.get('dates', 'Dates')}*\n"
            desc = job.get("description", [])
            if isinstance(desc, list):
                for bullet in desc:
                    md += f"* {bullet}\n"
            md += "\n"
        elif isinstance(job, str):
            md += f"* {job}\n"
    md += "\n"

    # Education
    md += "## Education\n\n"
    edu = structured_data.get("education", [])
    for e in edu:
        if isinstance(e, dict):
            md += f"### {e.get('degree', 'Degree')} - {e.get('institution', 'Institution')}\n"
            md += f"*{e.get('dates', 'Dates')}*\n"
            if "description" in e:
                md += f"{e['description']}\n"
            md += "\n"
        elif isinstance(e, str):
            md += f"* {e}\n"
    md += "\n"

    # Skills
    md += "## Skills\n\n"
    skills = structured_data.get("skills", [])
    md += ", ".join(skills) + "\n\n"

    buf = BytesIO(md.encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="ats_optimized_resume.md",
        mimetype="text/markdown",
    )


@app.route("/generate_resume_pdf", methods=["POST"])
def generate_resume_pdf():
    """Generate a simple PDF from structured resume data (used by 'Generate PDF' button)."""
    try:
        from reportlab.pdfgen import canvas  # ensure import
    except ImportError:
        return jsonify({"error": "reportlab not installed on server."}), 500

    data = request.get_json()
    structured_data = data.get("structured_data")

    if not structured_data:
        return jsonify({"error": "No structured data provided."}), 400

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50

    name = structured_data.get("name", "Your Name")
    email = structured_data.get("email", "")
    phone = structured_data.get("phone", "")
    linkedin = structured_data.get("linkedin", "")
    contact_line = " | ".join(x for x in [email, phone, linkedin] if x)

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, name)
    y -= 20

    c.setFont("Helvetica", 10)
    if contact_line:
        c.drawString(50, y, contact_line)
        y -= 30

    def heading(t):
        nonlocal y
        c.setFont("Helvetica-Bold", 12)
        c.drawString(50, y, t)
        y -= 18
        c.setFont("Helvetica", 10)

    def wrap(text, indent=0):
        nonlocal y
        if not text:
            return
        max_chars = 95 - indent
        line = text
        while len(line) > max_chars:
            c.drawString(50 + indent * 4, y, line[:max_chars])
            y -= 14
            line = line[max_chars:]
        c.drawString(50 + indent * 4, y, line)
        y -= 14

    # Summary
    summary = structured_data.get("summary", "")
    if summary:
        heading("Profile Summary")
        for line in summary.split("\n"):
            if line.strip():
                wrap(line)
        y -= 6

    # Experience
    exp = structured_data.get("experience", [])
    if exp:
        heading("Experience")
        for job in exp:
            if isinstance(job, dict):
                header = f"{job.get('title', 'Job Title')} @ {job.get('company', 'Company')} {job.get('dates', '')}"
                wrap(header)
                desc = job.get("description", [])
                if isinstance(desc, list):
                    for bullet in desc:
                        wrap("• " + bullet, indent=2)
                y -= 4
            elif isinstance(job, str):
                wrap("• " + job)
        y -= 6

    # Education
    edu = structured_data.get("education", [])
    if edu:
        heading("Education")
        for e in edu:
            if isinstance(e, dict):
                header = f"{e.get('degree', 'Degree')} - {e.get('institution', 'Institution')} {e.get('dates', '')}"
                wrap(header)
                if e.get("description"):
                    wrap(e["description"], indent=2)
            elif isinstance(e, str):
                wrap("• " + e)
        y -= 6

    # Skills
    skills = structured_data.get("skills", [])
    if skills:
        heading("Skills")
        wrap(", ".join(skills))

    c.showPage()
    c.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="resume.pdf",
        mimetype="application/pdf",
    )


@app.route("/generate_docx", methods=["POST"])
def generate_docx():
    data = request.get_json()
    structured_data = data.get("structured_data")
    if not structured_data:
        return jsonify({"error": "No structured data provided."}), 400

    doc_buf = generate_docx_file(structured_data)
    if not doc_buf:
        return jsonify({"error": "DOCX generation failed (python-docx missing?)."}), 500

    return send_file(
        doc_buf,
        as_attachment=True,
        download_name="resume.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
