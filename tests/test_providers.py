import pytest
from unittest.mock import patch, MagicMock
from agentic_memory.models import FactRecord, Scope
from agentic_memory.providers import (
    FastEmbedEmbedder,
    OpenAICompatibleEmbedder,
    OllamaExtractor,
    OpenAICompatibleExtractor
)

def test_fastembed_embedder():
    embedder = FastEmbedEmbedder(model_name="BAAI/bge-small-en-v1.5")
    assert embedder.dimension == 384
    vectors = embedder.embed(["Hello world", "Test sentence"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 384
    assert isinstance(vectors[0][0], float)

@patch("requests.post")
def test_openai_compatible_embedder(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": [
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            {"index": 1, "embedding": [0.4, 0.5, 0.6]}
        ]
    }
    mock_post.return_value = mock_resp

    embedder = OpenAICompatibleEmbedder(
        base_url="http://mock-proxy:8080/v1",
        model="text-embedding-3-small",
        api_key="test-key",
        dimension=3
    )
    assert embedder.dimension == 3

    embeddings = embedder.embed(["doc1", "doc2"])
    assert len(embeddings) == 2
    assert embeddings[0] == [0.1, 0.2, 0.3]
    assert embeddings[1] == [0.4, 0.5, 0.6]
    mock_post.assert_called_once()

@pytest.mark.asyncio
@patch("requests.post")
async def test_openai_compatible_extractor(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"facts": [{"subject": "user", "predicate": "favorite_color", "object_value": "blue", "confidence": 0.98}]}'
                }
            }
        ]
    }
    mock_post.return_value = mock_resp

    extractor = OpenAICompatibleExtractor(
        base_url="http://mock-proxy:8080/v1",
        model="gpt-4o-mini",
        api_key="test-key"
    )

    facts = await extractor.extract_facts("My favorite color is blue.")
    assert len(facts) == 1
    assert facts[0].subject == "user"
    assert facts[0].predicate == "favorite_color"
    assert facts[0].object_value == "blue"
    assert facts[0].confidence == 0.98
