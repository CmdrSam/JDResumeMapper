"""Deterministic skill aliases (normalization layer before/after LLM)."""

ALIASES: dict[str, str] = {
    "js": "JavaScript",
    "javascript": "JavaScript",
    "ts": "TypeScript",
    "typescript": "TypeScript",
    "py": "Python",
    "python3": "Python",
    "python": "Python",
    "k8s": "Kubernetes",
    "k8": "Kubernetes",
    "kubernetes": "Kubernetes",
    "tf": "Terraform",
    "terraform": "Terraform",
    "aws": "AWS",
    "amazon web services": "AWS",
    "gcp": "GCP",
    "google cloud": "GCP",
    "azure": "Azure",
    "ms azure": "Azure",
    "node": "Node.js",
    "nodejs": "Node.js",
    "node.js": "Node.js",
    "react.js": "React",
    "reactjs": "React",
    "vue.js": "Vue",
    "angular.js": "Angular",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mongo": "MongoDB",
    "mongodb": "MongoDB",
    "ci/cd": "CI/CD",
    "cicd": "CI/CD",
}


def normalize_token(skill: str) -> str:
    key = skill.strip().lower()
    return ALIASES.get(key, skill.strip())


def normalize_skill_list(skills: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in skills:
        n = normalize_token(s)
        if n and n.lower() not in {x.lower() for x in seen}:
            seen.add(n)
            out.append(n)
    return out
