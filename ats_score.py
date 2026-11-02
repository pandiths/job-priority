#!/usr/bin/env python3
"""ATS-style keyword coverage score: how many skill terms in a job description appear in a resume.

usage: python3 ats_score.py <resume.html|.txt> <jd.txt> [--json]

Score = weighted share of JD skill terms present in the resume (terms in the
'required/minimum' part of the JD count double). Also reports which missing
terms you can honestly claim (CLAIMABLE) vs. ones you can't (do not add).
"""
import json
import os
import re
import sys

# canonical term -> regex alternatives (case-insensitive, word-bounded)
VOCAB = {
    "Python": r"python", "Java": r"java(?!script)", "JavaScript": r"javascript|\bjs\b", "TypeScript": r"typescript",
    "Go": r"golang|\bgo\b(?= |,|/|\))", "C++": r"c\+\+", "SQL": r"\bsql\b", "Bash": r"bash|shell scripting",
    "Kotlin": r"kotlin", "Rust": r"\brust\b", "Scala": r"\bscala\b", "C#": r"c#", "Ruby": r"\bruby\b|rails",
    "Node.js": r"node\.?js|\bnode\b", "React": r"react", "Next.js": r"next\.?js", "Spring Boot": r"spring ?boot|\bspring\b",
    "FastAPI": r"fastapi", "Django": r"django", "Flask": r"flask",
    "REST APIs": r"\brest(ful)?\b|rest apis?", "GraphQL": r"graphql", "gRPC": r"grpc", "APIs": r"\bapis?\b",
    "microservices": r"micro-?services?", "distributed systems": r"distributed (systems?|computing)",
    "backend": r"back-?end", "full stack": r"full[- ]?stack", "frontend": r"front-?end",
    "PostgreSQL": r"postgres(ql)?", "MySQL": r"mysql", "MongoDB": r"mongo(db)?", "Redis": r"redis", "Firestore": r"firestore",
    "DynamoDB": r"dynamo ?db", "Elasticsearch": r"elastic ?search", "Cassandra": r"cassandra",
    "Kafka": r"kafka", "Spark": r"\bspark\b|pyspark", "Airflow": r"airflow", "Flink": r"flink", "Hadoop": r"hadoop",
    "data pipelines": r"data pipelines?|etl|pipelines?", "databases": r"databases?",
    "AWS": r"\baws\b|amazon web services", "GCP": r"\bgcp\b|google cloud", "Azure": r"azure",
    "Docker": r"docker|containers?", "Kubernetes": r"kubernetes|\bk8s\b", "Terraform": r"terraform", "Helm": r"\bhelm\b",
    "CI/CD": r"ci/?cd|continuous (integration|delivery|deployment)", "GitHub Actions": r"github actions", "Jenkins": r"jenkins",
    "Linux": r"linux|unix", "Git": r"\bgit\b", "serverless": r"serverless|lambda|cloud run",
    "observability": r"observability|monitoring|logging|metrics|alerting|prometheus|grafana|datadog",
    "incident response": r"incident|on-?call|root[- ]cause", "reliability": r"reliab|uptime|\bslo|\bsla",
    "scalability": r"scal(able|ability|ing)", "performance optimization": r"latency|performance|optimi[sz]",
    "testing": r"testing|unit tests?|test-driven|\btdd\b|tested", "code review": r"code reviews?",
    "machine learning": r"machine learning|\bml\b", "deep learning": r"deep learning|neural net",
    "LLMs": r"\bllms?\b|large language models?|generative ai|genai|gen ai", "AI agents": r"agents?\b|agentic",
    "RAG": r"\brag\b|retrieval[- ]augmented", "prompt engineering": r"prompt(ing| engineering)?",
    "embeddings": r"embeddings?|vector (search|database|store)", "NLP": r"\bnlp\b|natural language",
    "computer vision": r"computer vision|\bcv\b",
    "image processing": r"image (processing|analysis)", "object detection": r"object detection|detector|yolo|\bssd\b",
    "image classification": r"image classif|classifier", "image segmentation": r"segmentation",
    "image registration": r"image registration|registration", "camera calibration": r"camera calibration|calibration",
    "multi-view geometry": r"multi-?view geometry|epipolar|stereo|structure from motion", "signal processing": r"signal processing",
    "OpenCV": r"opencv", "geometry/homography": r"homograph|perspective transform|projective geometry",
    "algorithm development": r"algorithm(s| development| design)", "benchmarking": r"benchmark", "validation": r"validat",
    "publications": r"publications?\b|cvpr|iccv|eccv|icip|neurips", "reinforcement learning": r"reinforcement learning|\brl\b",
    "model evaluation": r"evaluat(e|ion|ing)|benchmark", "model deployment": r"deploy(ing|ment|ed)? (ml |machine learning )?models?|productioni[sz]|inference",
    "fine-tuning": r"fine-?tun|transfer learning", "PyTorch": r"pytorch|torch", "TensorFlow": r"tensorflow|keras",
    "scikit-learn": r"scikit|sklearn", "XGBoost": r"xgboost", "pandas": r"pandas", "NumPy": r"numpy",
    "MLOps": r"mlops|ml ops|ml infrastructure|ml platform", "experimentation": r"experiment|a/b test",
    "statistics": r"statistic", "recommendation systems": r"recommend(ation|er) systems?|recommendations?|ranking",
    "classification": r"classif", "clustering": r"cluster(ing)?", "feature engineering": r"feature (engineering|store|platform)",
    "Claude/Anthropic": r"claude|anthropic", "OpenAI": r"openai|gpt", "LangChain": r"langchain|langgraph",
    "AI coding tools": r"cursor|copilot|claude code|codex|ai[- ](coding|assisted)",
    "webhooks": r"webhooks?", "event-driven": r"event[- ](driven|sourc)|streaming|pub/?sub",
    "security": r"security|auth(entication|orization)|iam\b", "cloud": r"\bcloud\b",
    "agile": r"agile|scrum", "system design": r"system design|architect", "ownership": r"ownership|end[- ]to[- ]end",
    "communication": r"communicat", "collaboration": r"collaborat|cross-functional",
}

# Terms you can truthfully add to a resume come from the private profile (see profile.example.json).
_PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "profile.json")
CLAIMABLE = set(json.load(open(_PROFILE)).get("claimable", [])) if os.path.exists(_PROFILE) else set()

REQ_START = re.compile(r"(minimum|basic|required|requirements|qualifications|what you.ll need|you have|who you are|who you might be|you will bring|we.re excited about you|what we.re looking for|about you)", re.I)
REQ_END = re.compile(r"(preferred|nice to have|bonus|plus|benefits|compensation|salary|pay range|perks|equal opportunity)", re.I)


def text_of(path):
    raw = open(path, encoding="utf-8", errors="ignore").read()
    if path.endswith(".html"):
        raw = re.sub(r"<style.*?</style>", " ", raw, flags=re.S)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = raw.replace("&amp;", "&")
    return re.sub(r"\s+", " ", raw)


def terms_in(text):
    return {k for k, pat in VOCAB.items() if re.search(pat, text, re.I)}


def score(resume_text, jd_text):
    jd_terms = terms_in(jd_text)
    m = REQ_START.search(jd_text)
    req_text = ""
    if m:
        tail = jd_text[m.start():]
        e = REQ_END.search(tail, 40)
        req_text = tail[: e.start()] if e else tail[:2500]
    req_terms = terms_in(req_text)
    have = terms_in(resume_text)
    w = {t: (2 if t in req_terms else 1) for t in jd_terms}
    total = sum(w.values()) or 1
    got = sum(v for t, v in w.items() if t in have)
    missing = sorted(jd_terms - have, key=lambda t: -w[t])
    return {
        "score": round(100 * got / total, 1),
        "jd_terms": len(jd_terms),
        "required_terms": sorted(req_terms),
        "missing_claimable": [t for t in missing if t in CLAIMABLE],
        "missing_not_claimable": [t for t in missing if t not in CLAIMABLE],
        "max_honest_score": round(100 * (got + sum(w[t] for t in missing if t in CLAIMABLE)) / total, 1),
    }


if __name__ == "__main__":
    r = score(text_of(sys.argv[1]), text_of(sys.argv[2]))
    print(json.dumps(r) if "--json" in sys.argv else json.dumps(r, indent=1))
