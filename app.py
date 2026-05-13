"""
Prompt Builder (single-file)

Requirements:
- Python 3.8+
- Install: `pip install langchain requests python-dotenv`

Usage:
1. Set your API key in environment variable `TOGETHER_API_KEY` (or the script will prompt you).
2. Run: `python app.py` and follow prompts.

This script uses LangChain by implementing a minimal `LLM` wrapper that calls the
Together API (https://api.together.xyz/v1) using OpenAI-compatible chat completions.
"""

import os
import sys
import json
import asyncio
import requests
from typing import Optional
import tempfile
import shutil

# Models to query in parallel
MODELS = [
    "openai/gpt-oss-120b",
    "deepseek-ai/DeepSeek-V4-Pro",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
]

# Document parsing libs (optional runtime deps)
try:
    import fitz  # pymupdf
except Exception:
    fitz = None

try:
    import docx  # python-docx
except Exception:
    docx = None

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, FileResponse
    from fastapi.middleware.cors import CORSMiddleware
except Exception:
    FastAPI = None

# Auto-load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
    print('Loaded .env (if present)')
except Exception:
    print('python-dotenv not installed; to auto-load .env, run: pip install python-dotenv')

try:
    from langchain.llms.base import LLM
except Exception:
    LLM = None


class TogetherLLM:
    """Lightweight client that speaks an OpenAI-compatible chat completion API on
    the Together base URL. This class acts as a thin adapter and falls back to a
    direct request if LangChain is not available.
    """

    def __init__(self, api_key: str, base_url: str = "https://api.together.xyz/v1", model: str = MODELS[0], timeout: int = 30):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str, max_tokens: int = 4096, temperature: float = 0.2) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        print(f"[LLM] --> Sending request to {url}")
        print(f"[LLM]     Model : {self.model}")
        print(f"[LLM]     Prompt length : {len(prompt)} chars")

        try:
            r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        except Exception as e:
            raise RuntimeError(f"Request to Together failed: {e}")

        print(f"[LLM] <-- Response status: {r.status_code}")

        if r.status_code != 200:
            # Show helpful debugging information
            raise RuntimeError(f"Together API returned status {r.status_code}: {r.text}")

        data = r.json()

        # Attempt to extract common response shapes
        text = None
        try:
            # OpenAI-style: choices[0].message.content
            text = data.get('choices', [])[0].get('message', {}).get('content')
        except Exception:
            text = None

        if not text:
            try:
                # older style: choices[0].text
                text = data.get('choices', [])[0].get('text')
            except Exception:
                text = None

        if not text:
            # fallback: entire output or generated text key
            text = data.get('output') or data.get('generated_text') or json.dumps(data, indent=2)

        print(f"[LLM]     Response length: {len(text or '')} chars")
        return text


def multi_line_input(prompt: str) -> str:
    print(prompt + " (finish with an empty line)")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "":
            break
        lines.append(line)
    return '\n'.join(lines).strip()


def extract_text_from_pdf(path: str) -> str:
    if fitz is None:
        raise RuntimeError('pymupdf (fitz) not installed — install with: pip install pymupdf')
    doc = fitz.open(path)
    parts = []
    for page in doc:
        parts.append(page.get_text())
    return '\n'.join(parts).strip()


def extract_text_from_docx(path: str) -> str:
    if docx is None:
        raise RuntimeError('python-docx not installed — install with: pip install python-docx')
    doc = docx.Document(path)
    paragraphs = [p.text for p in doc.paragraphs if p.text]
    return '\n'.join(paragraphs).strip()


def extract_document_text(path: str) -> str:
    if not path:
        return ''
    path = path.strip()
    if not os.path.exists(path):
        raise FileNotFoundError(f'Document not found: {path}')
    lower = path.lower()
    if lower.endswith('.pdf'):
        return extract_text_from_pdf(path)
    if lower.endswith('.docx') or lower.endswith('.doc'):
        return extract_text_from_docx(path)
    # fallback: try to read as text
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        raise RuntimeError('Unsupported document type or failed to read document')


def compose_final_prompt(role: str, context: str, output: str, doc_text: str) -> str:
    parts = []
    parts.append(f'Role: {role.strip()}')
    parts.append('\nContext:')
    parts.append(context.strip() or '- no context provided -')
    parts.append('\nRequested Output:')
    parts.append(output.strip() or '- no output description provided -')
    if doc_text:
        parts.append('\nDocument Context:')
        parts.append(doc_text.strip())
    parts.append('\n\nPlease produce the requested output using the information above. Provide the final response only.')
    parts.append('[ignoring loop detection]')
    return '\n'.join(parts)


def build_generation_prompt(role: str, persona: str, objective: str, expected_outcome: str, steps: str, keep_in_mind: str) -> str:
    # This is the prompt we will send to the model to produce a detailed markdown prompt
    return f"""
You are an expert prompt engineer. Using the user inputs below, create a single well-structured, detailed Markdown prompt that someone can copy-paste to instruct a helpful coding assistant.

Inputs:
- Role: {role}
- Persona / Focus: {persona}
- Objective (the "Why"): {objective}
- Expected Outcome: {expected_outcome}
- Specific Steps (if any):
{steps or '- none provided -'}
- Keep In Mind / Constraints:
{keep_in_mind or '- none provided -'}

Requirements for the output:
- Format the result in Markdown.
- Include these sections with headings: `Role & System Identity`, `Project Context`, `Task Description`, `Expected Outcome`, `Execution Steps`, `Constraints / Keep In Mind`, and `Final Prompt`.
- In `Execution Steps` present a clear numbered list with any assumptions and concrete sub-steps.
- In `Final Prompt` provide a concise ready-to-use prompt (no more than 6-8 sentences) that captures intent and necessary constraints.
- Be explicit, use actionable language, and avoid apologetic or hedging phrases.

Produce only the Markdown output (no additional commentary).

[ignoring loop detection]
"""


def main():
    api_key = os.environ.get('TOGETHER_API_KEY')
    if not api_key:
        print('Environment variable TOGETHER_API_KEY is not set.')
        api_key = input('Enter your Together API key (will not be saved): ').strip()
        if not api_key:
            print('API key required. Set TOGETHER_API_KEY and rerun.')
            sys.exit(1)
    print('\nProvide the following inputs. For multi-line entries press Enter on an empty line to finish.\n')
    role = input('Role (one-line): ').strip()
    context = multi_line_input('Context (multi-line)')
    output = multi_line_input('Output description / desired response (multi-line)')
    doc_path = input('Optional document path (pdf/docx) — leave empty if none: ').strip()

    try:
        prompt, doc_text, response = generate_with_doc(role=role, context=context, output=output, document_path=doc_path, api_key=api_key)
    except Exception as e:
        print('Error:', e)
        sys.exit(1)

    # Save prompt and response
    try:
        with open('generated_prompt.txt', 'w', encoding='utf-8') as f:
            f.write(prompt + '\n\n---MODEL RESPONSE---\n\n' + (response or ''))
    except Exception:
        pass

    print('\n--- Final Prompt Sent to Model ---\n')
    print(prompt[:5000])
    print('\n--- Model Response ---\n')
    print(response)


def generate_with_doc(role: str, context: str, output: str, document_path: str = '', api_key: Optional[str] = None):
    """Compose prompt including optional document content, call model, and return (prompt, doc_text, response)."""
    if api_key is None:
        api_key = os.environ.get('TOGETHER_API_KEY')
    if not api_key:
        raise RuntimeError('TOGETHER_API_KEY is not set')

    doc_text = ''
    if document_path:
        print(f"[PIPELINE] Step 1 — Document received: {document_path}")
        doc_text = extract_document_text(document_path)
        print(f"[PIPELINE] Step 2 — Document text extracted: {len(doc_text)} chars")
        if doc_text:
            print(f"[PIPELINE]          Preview (first 200 chars): {doc_text[:200].strip()!r}")
    else:
        print("[PIPELINE] Step 1 — No document provided, skipping extraction")

    print("[PIPELINE] Step 3 — Composing final prompt...")
    final_prompt = compose_final_prompt(role=role, context=context, output=output, doc_text=doc_text)
    print(f"[PIPELINE]          Prompt length: {len(final_prompt)} chars")
    print(f"[PIPELINE]          Prompt preview (first 300 chars):\n{final_prompt[:300].strip()}")

    print("[PIPELINE] Step 4 — Sending prompt + document context to LLM...")
    client = TogetherLLM(api_key=api_key, base_url='https://api.together.xyz/v1', model=MODELS[0])
    response = client.generate(final_prompt)
    print(f"[PIPELINE] Step 5 — LLM response received: {len(response or '')} chars")
    print(f"[PIPELINE]          Response preview (first 300 chars):\n{(response or '')[:300].strip()}")
    return final_prompt, doc_text, response
def generate_markdown(role: str, persona: str, objective: str, expected_outcome: str, steps: str, keep_in_mind: str, api_key: Optional[str] = None) -> str:
    """Callable function: generate the detailed markdown prompt using the Together model.

    If `api_key` is None, reads `TOGETHER_API_KEY` from the environment.
    """
    if api_key is None:
        api_key = os.environ.get('TOGETHER_API_KEY')
    if not api_key:
        raise RuntimeError('TOGETHER_API_KEY is not set')

    generation_prompt = build_generation_prompt(role, persona, objective, expected_outcome, steps, keep_in_mind)
    client = TogetherLLM(api_key=api_key, base_url='https://api.together.xyz/v1', model=MODELS[0])
    return client.generate(generation_prompt)


# --- FastAPI server (optional) -------------------------------------------------
if FastAPI is not None:
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]) 

    @app.get("/")
    def index():
        return FileResponse('app.html')

    @app.post('/generate')
    async def generate_api(request: Request):
        tmp_file_paths = []
        document_paths = []
        try:
            content_type = request.headers.get('content-type', '')
            print(f"\n{'='*60}")
            print(f"[REQUEST] POST /generate  |  Content-Type: {content_type[:60]}")

            # --- Parse request ---
            if content_type and 'multipart/form-data' in content_type:
                print("[REQUEST] Parsing multipart/form-data (file upload mode)")
                form = await request.form()
                role = form.get('role', '')
                context = form.get('context', '')
                output = form.get('output', '')
                uploads = []
                if hasattr(form, 'getlist'):
                    uploads = form.getlist('document') or []
                if not uploads:
                    upload = form.get('document')
                    if upload is not None:
                        uploads = [upload]

                for upload in uploads:
                    if upload is not None and hasattr(upload, 'filename') and upload.filename:
                        suffix = os.path.splitext(upload.filename)[1] or ''
                        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                        tmp_file_path = tmp.name
                        with open(tmp_file_path, 'wb') as f:
                            shutil.copyfileobj(upload.file, f)
                        tmp_file_paths.append(tmp_file_path)
                        document_paths.append(tmp_file_path)
                        print(f"[REQUEST] Document saved to temp file: {tmp_file_path}")

                if not document_paths:
                    print("[REQUEST] No file attached in form")
            else:
                print("[REQUEST] Parsing JSON body")
                try:
                    payload = await request.json()
                except Exception:
                    payload = {}
                role = payload.get('role', '')
                context = payload.get('context', '')
                output = payload.get('output', '')
                document_paths = payload.get('document_paths') or []
                document_path = payload.get('document_path', '')
                if document_path and not document_paths:
                    document_paths = [document_path]
                if isinstance(document_paths, str):
                    document_paths = [document_paths]
                document_paths = [p for p in document_paths if p]

            if len(document_paths) > 5:
                return JSONResponse({'error': 'You can upload up to 5 documents.'}, status_code=400)

            print(f"[REQUEST] role={role!r}  |  context_len={len(context)}  |  output_len={len(output)}")

            # --- Get API key ---
            api_key = os.environ.get('TOGETHER_API_KEY')
            if not api_key:
                return JSONResponse({'error': 'TOGETHER_API_KEY is not set'}, status_code=500)

            # --- Step 1 & 2: Extract document once ---
            doc_text = ''
            if document_paths:
                print(f"[PIPELINE] Step 1 — {len(document_paths)} document(s) received")
                doc_text_parts = []
                for idx, path in enumerate(document_paths, start=1):
                    print(f"[PIPELINE] Step 2.{idx} — Extracting: {path}")
                    try:
                        text = extract_document_text(path)
                        doc_text_parts.append(text)
                        print(f"[PIPELINE]          Extracted: {len(text)} chars")
                        if text:
                            print(f"[PIPELINE]          Preview: {text[:200].strip()!r}")
                    except Exception as e:
                        print(f"[PIPELINE] Step 2.{idx} — Document extraction failed: {e}")
                        return JSONResponse({'error': str(e)}, status_code=500)
                doc_text = '\n\n'.join([t for t in doc_text_parts if t])
            else:
                print("[PIPELINE] Step 1 — No document provided")

            # --- Step 3: Compose prompt once ---
            print("[PIPELINE] Step 3 — Composing final prompt...")
            final_prompt = compose_final_prompt(role=role, context=context, output=output, doc_text=doc_text)
            print(f"[PIPELINE]          Prompt length: {len(final_prompt)} chars")
            print(f"[PIPELINE]          Preview:\n{final_prompt[:300].strip()}")

            # --- Step 4: Call all 3 models in parallel ---
            print(f"[PIPELINE] Step 4 — Firing {len(MODELS)} models in parallel...")

            async def call_model(model_name: str) -> str:
                print(f"[LLM] --> {model_name} | prompt {len(final_prompt)} chars")
                client = TogetherLLM(
                    api_key=api_key,
                    base_url='https://api.together.xyz/v1',
                    model=model_name,
                    timeout=60,
                )
                result = await asyncio.to_thread(client.generate, final_prompt)
                print(f"[LLM] <-- {model_name} | {len(result or '')} chars received")
                return result

            results = await asyncio.gather(
                *[call_model(m) for m in MODELS],
                return_exceptions=True,
            )

            responses = {}
            for model, result in zip(MODELS, results):
                if isinstance(result, Exception):
                    print(f"[ERROR] {model} failed: {result}")
                    responses[model] = f"Error: {result}"
                else:
                    responses[model] = result

            print(f"[PIPELINE] Step 5 — All models responded. Returning to client.")
            print(f"{'='*60}\n")
            return JSONResponse({'prompt': final_prompt, 'document_text': doc_text, 'responses': responses})

        except Exception as e:
            print(f"[ERROR] Unhandled exception: {e}")
            return JSONResponse({'error': str(e)}, status_code=500)
        finally:
            for tmp_file_path in tmp_file_paths:
                if tmp_file_path and os.path.exists(tmp_file_path):
                    try:
                        os.remove(tmp_file_path)
                    except Exception:
                        pass


if __name__ == '__main__':
    # run server with uvicorn (--serve) or CLI otherwise
    if '--serve' in sys.argv:
        try:
            import uvicorn
        except Exception:
            print('uvicorn is not installed. Install with: pip install uvicorn fastapi')
            sys.exit(1)

        api_key = os.environ.get('TOGETHER_API_KEY')
        if not api_key:
            print('Environment variable TOGETHER_API_KEY is not set.')
            api_key = input('Enter your Together API key (will not be saved): ').strip()
            if not api_key:
                print('API key required. Set TOGETHER_API_KEY and rerun.')
                sys.exit(1)

        # Run uvicorn programmatically
        print('Starting server on http://127.0.0.1:5000')
        uvicorn.run('app:app', host='127.0.0.1', port=5000, log_level='info')
    else:
        main()