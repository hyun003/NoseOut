"""EmbeddingGemma 2 작업 프롬프트. 원본 DigUp(Sources/DigUpKit/Model.swift)과 모델 카드의 형식을 따른다.
이미지·음성에는 프롬프트를 붙이지 않는다."""


def query(text: str) -> str:
    return f"task: search result | query: {text}"


def code_query(text: str) -> str:
    return f"task: code retrieval | query: {text}"


def document(text: str, title: str | None = None) -> str:
    return f"title: {title or 'none'} | text: {text}"
