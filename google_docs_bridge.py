import json
import os
import re
from typing import Any


DOC_ID_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")


def extract_google_doc_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    match = DOC_ID_RE.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", text):
        return text
    return ""


def _service_account_info() -> dict[str, Any]:
    raw_json = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    file_path = (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
    if raw_json:
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc
    if file_path:
        if not os.path.exists(file_path):
            raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_FILE does not exist: {file_path}")
        with open(file_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    raise RuntimeError(
        "Google Docs is not configured. Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE."
    )


def service_account_email() -> str:
    try:
        info = _service_account_info()
    except Exception:
        return ""
    return (info.get("client_email") or "").strip()


def google_docs_ready() -> tuple[bool, str]:
    try:
        info = _service_account_info()
        email = (info.get("client_email") or "").strip()
        if not email:
            return False, "Google service account JSON is missing client_email."
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _docs_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    info = _service_account_info()
    creds = Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def _read_structural_elements(elements: list[dict[str, Any]], pieces: list[str]):
    for element in elements or []:
        paragraph = element.get("paragraph")
        if paragraph:
            text = "".join(
                item.get("textRun", {}).get("content", "")
                for item in paragraph.get("elements", [])
            ).rstrip("\n")
            pieces.append(text)
        table = element.get("table")
        if table:
            for row in table.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    _read_structural_elements(cell.get("content", []), pieces)
        toc = element.get("tableOfContents")
        if toc:
            _read_structural_elements(toc.get("content", []), pieces)


def fetch_google_doc(url_or_id: str, *, max_chars: int = 12000, without_suggestions: bool = False) -> dict[str, Any]:
    """``max_chars=0`` reads the whole document. ``without_suggestions`` reads it as if every pending suggestion were
    rejected: an editor account otherwise gets suggestions inline, so text a commenter merely proposed would count as
    part of the document."""
    doc_id = extract_google_doc_id(url_or_id)
    if not doc_id:
        raise ValueError("That is not a valid Google Docs link or document id.")
    service = _docs_service()
    options = {"suggestionsViewMode": "PREVIEW_WITHOUT_SUGGESTIONS"} if without_suggestions else {}
    try:
        doc = service.documents().get(documentId=doc_id, **options).execute()
    except Exception as exc:
        email = service_account_email()
        if email:
            raise RuntimeError(
                f"Couldn't open that Google Doc. Share it with `{email}` as an editor first."
            ) from exc
        raise
    pieces: list[str] = []
    _read_structural_elements(doc.get("body", {}).get("content", []), pieces)
    text = "\n".join(pieces).strip()
    if max_chars > 0:
        text = text[:max_chars]
    return {
        "doc_id": doc_id,
        "title": (doc.get("title") or "Untitled document").strip() or "Untitled document",
        "text": text,
    }


def overwrite_google_doc(url_or_id: str, new_text: str) -> dict[str, Any]:
    doc_id = extract_google_doc_id(url_or_id)
    if not doc_id:
        raise ValueError("That is not a valid Google Docs link or document id.")
    text = (new_text or "").replace("\r\n", "\n").strip("\n")
    if not text.strip():
        raise ValueError("Refusing to overwrite the document with empty text.")
    service = _docs_service()
    doc = service.documents().get(documentId=doc_id).execute()
    body_content = doc.get("body", {}).get("content", [])
    end_index = 1
    if body_content:
        end_index = int(body_content[-1].get("endIndex", 1) or 1)
    requests: list[dict[str, Any]] = []
    if end_index > 2:
        requests.append(
            {
                "deleteContentRange": {
                    "range": {
                        "startIndex": 1,
                        "endIndex": end_index - 1,
                    }
                }
            }
        )
    requests.append(
        {
            "insertText": {
                "location": {"index": 1},
                "text": text + "\n",
            }
        }
    )
    service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
    return {
        "doc_id": doc_id,
        "title": (doc.get("title") or "Untitled document").strip() or "Untitled document",
        "chars_written": len(text),
    }
