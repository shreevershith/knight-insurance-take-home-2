"""One schema-driven LLM extractor for every non-tabular page (text layer or vision transcript)."""
from app import llm
from app.db import Page
from app.extract.spec import SPEC

SYSTEM = """You extract data from one page of a commercial auto insurance submission.

Rules:
- Extract only what is printed on THIS page. Never infer, calculate, or fill in from general knowledge.
- `value`: the value exactly as printed (keep original date/number formats, typos and abbreviations).
- `excerpt`: copied VERBATIM from the <page_text> block: the shortest contiguous span that contains
  the value, including its label when the label is on the same line. Do not paraphrase or reformat it.
  If a value is only visible in the image (e.g. a checkbox state), set excerpt to the text of the
  option label as it appears in <page_text>.
- Omit fields that are not on the page. Do not output empty values.
- Yes/no fields: answer "yes" or "no" based on which box is checked / which answer is marked.
  In this text layer a checked box may appear as the letter "n" or "■" next to the option.
- Lists (drivers, vehicles, claims, rows...): one item per real row. Skip placeholder text such as
  "LIST ATTACHED" and blank template rows.
"""


def _group_schema(fields: dict) -> dict:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "enum": list(fields)},
                "value": {"type": "string"},
                "excerpt": {"type": "string"},
            },
            "required": ["field", "value", "excerpt"],
            "additionalProperties": False,
        },
    }


def build_schema(doc_type: str) -> dict:
    props = {}
    for group, (_, is_list, fields) in SPEC[doc_type].items():
        facts = _group_schema(fields)
        if is_list:
            props[group] = {"type": "array", "items": {
                "type": "object", "properties": {"facts": facts},
                "required": ["facts"], "additionalProperties": False}}
        else:
            props[group] = facts
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _field_guide(doc_type: str) -> str:
    lines = []
    for group, (_, is_list, fields) in SPEC[doc_type].items():
        lines.append(f"{group} ({'list of items' if is_list else 'single'}):")
        lines.extend(f"  - {name}: {desc}" for name, desc in fields.items())
    return "\n".join(lines)


def extract_page(page: Page, doc_type: str, use_image: bool, ctx: dict) -> list[dict]:
    """Returns [{group, entity_type, index, field, value, excerpt}]. Raises llm.LLMError."""
    content = []
    if use_image and page.image_path:
        content.append(llm.image_block(page.image_path))
    content.append({"type": "text", "text": (
        f"Document type: {doc_type}\nFields to extract:\n{_field_guide(doc_type)}\n\n"
        f"<page_text>\n{page.text}\n</page_text>")})
    data = llm.structured_call(task=f"extract_{doc_type.lower()}", system=SYSTEM, content=content,
                               schema=build_schema(doc_type), ctx=ctx)
    out = []
    for group, (entity_type, is_list, _) in SPEC[doc_type].items():
        items = data.get(group) or []
        if not is_list:
            items = [{"facts": items}]
        for index, item in enumerate(items):
            for fact in item.get("facts", []):
                if str(fact.get("value", "")).strip():
                    out.append({"group": group, "entity_type": entity_type, "index": index,
                                "field": fact["field"], "value": str(fact["value"]).strip(),
                                "excerpt": str(fact.get("excerpt", "")).strip()})
    return out
