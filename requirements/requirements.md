Here’s a clean, **engineering-focused, step-by-step guide** to build this Python project end-to-end. I’ll keep it practical so you can actually implement it.

---

# 🧠 Project Overview

You’ll build a pipeline that:

1. **Parses resumes (PDFs)** → extracts structured data
2. **Parses Job Descriptions (JDs)** → extracts required skills
3. Uses an LLM to:

   * Normalize skills
   * Match candidates to JDs
   * Generate reasoning + ratings
4. Outputs:

   * Candidate-wise scoring tables
   * Optionally CSV / UI

---

# ⚙️ Step 1: Setup Project

### Create project structure

```
resume_matcher/
│
├── data/
│   ├── resumes/
│   ├── jds/
│
├── src/
│   ├── parser/
│   ├── extractor/
│   ├── llm/
│   ├── matcher/
│   ├── utils/
│
├── outputs/
│
├── main.py
├── requirements.txt
```

---

### Install dependencies

```bash
pip install pdfplumber python-docx spacy pandas openai tiktoken python-dotenv
```

(Optional but recommended)

```bash
pip install langchain
```

---

# 📄 Step 2: Resume PDF Parsing

Use `pdfplumber` (more reliable than PyPDF for text extraction).

### `src/parser/resume_parser.py`

```python
import pdfplumber

def extract_text_from_pdf(file_path):
    text = ""
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() + "\n"
    return text
```

---

# 🔍 Step 3: Extract Structured Resume Data

You’ll extract:

* Name
* Email
* Phone
* Address
* Experience
* Skills

---


### Use LLM for structured extraction

```python
def extract_resume_with_llm(llm, text):
    prompt = f"""
    Extract structured information from this resume:

    {text}

    Return JSON:
    {{
        "name": "",
        "email": "",
        "phone": "",
        "skills": [],
        "experience": [
            {{
                "company": "",
                "role": "",
                "duration": "",
                "description": ""
            }}
        ]
    }}
    """
    return llm.invoke(prompt)
```

---

# 📌 Step 4: Parse Job Descriptions

### `src/parser/jd_parser.py`

```python
def load_jd(file_path):
    with open(file_path, "r") as f:
        return f.read()
```

---

# 🧠 Step 5: Extract Required Skills from JD

Use LLM for normalization.

```python
def extract_jd_skills(llm, jd_text):
    prompt = f"""
    Extract required skills from this job description.

    Categorize them like:
    - Programming Languages
    - Frameworks
    - Tools
    - Soft Skills

    JD:
    {jd_text}

    Return JSON:
    {{
        "skills": [
            {{
                "category": "",
                "skill": "",
                "importance": "high/medium/low"
            }}
        ]
    }}
    """
    return llm.invoke(prompt)
```

---

# 🤖 Step 6: Candidate Matching + Scoring

This is the **core intelligence layer**.

### Input:

* JD skills
* Candidate skills + experience

### Output:

* Rating (1–10)
* Reason

---

### `src/matcher/match_engine.py`

```python
def match_candidate_to_jd(llm, candidate, jd):
    prompt = f"""
    You are an expert recruiter.

    Job Requirements:
    {jd}

    Candidate Profile:
    {candidate}

    For each required skill:
    - Compare candidate experience
    - Provide reasoning
    - Give rating (1-10)

    Return as table JSON:
    [
        {{
            "Skill Category": "",
            "Skill": "",
            "JD Requirement": "",
            "Candidate Match Reason": "",
            "Rating": 0
        }}
    ]
    """
    return llm.invoke(prompt)
```

---

# 📊 Step 7: Generate Output Table

### Using pandas

```python
import pandas as pd

def create_table(data, candidate_name, jd_name):
    df = pd.DataFrame(data)
    output_path = f"outputs/{candidate_name}_{jd_name}.csv"
    df.to_csv(output_path, index=False)
    return df
```

---

# 🚀 Step 8: Main Pipeline

### `main.py`

```python
from src.parser.resume_parser import extract_text_from_pdf
from src.extractor.resume_extractor import extract_resume_with_llm
from src.parser.jd_parser import load_jd
from src.matcher.match_engine import match_candidate_to_jd

def run_pipeline(resume_files, jd_files, llm):
    results = []

    for jd_file in jd_files:
        jd_text = load_jd(jd_file)

        for resume_file in resume_files:
            text = extract_text_from_pdf(resume_file)
            candidate = extract_resume_with_llm(llm, text)

            match = match_candidate_to_jd(llm, candidate, jd_text)

            results.append({
                "candidate": resume_file,
                "jd": jd_file,
                "match": match
            })

    return results
```

---

# 🧩 Step 9: LLM Setup

### Option 1: OpenAI

```python
from openai import OpenAI

client = OpenAI()

def llm_invoke(prompt):
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content
```

---

### Option 2: Local (since you mentioned Ollama)

Use:

* `llama3`
* `mistral`

---

# 🧪 Step 10: Improvements (Important)

### 1. Skill Normalization Layer

Map:

* "JS" → "JavaScript"
* "Py" → "Python"

---

### 2. Embedding-based Matching (Advanced)

Use:

* cosine similarity between JD skills and resume content

Libraries:

```bash
pip install sentence-transformers faiss-cpu
```

---

### 3. Ranking Formula

Instead of raw LLM rating:

```
Final Score = 
0.5 * Skill Match +
0.3 * Experience Relevance +
0.2 * Seniority Fit
```

---

### 4. UI (Optional)

Since you’ve used Streamlit:

```bash
pip install streamlit
```

Build:

* Upload resumes
* Upload JD
* Show tables

---

# 📋 Final Output Example

| Skill Category | Skill  | JD Requirement    | Candidate Match Reason            | Rating |
| -------------- | ------ | ----------------- | --------------------------------- | ------ |
| Programming    | Python | Strong experience | 5 years backend work using Python | 9      |
| Framework      | Django | Required          | Used in 2 projects                | 7      |
| Cloud          | AWS    | Preferred         | Limited exposure                  | 5      |

---

# 🧠 Key Design Insight

* Use **LLM for reasoning**
* Use **code for structure + scaling**
* Avoid full reliance on LLM → add deterministic layers

---

# 👉 If you want next step

I can help you:

* Convert this into a **production-grade architecture (with LangChain + agents)**
* Add **multi-JD ranking dashboard**
* Or **optimize for Raspberry Pi deployment** (since you mentioned it earlier)

