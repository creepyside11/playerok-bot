import pytest
from app.ai_manager import UniversalImageClient, UniversalOpenAIClient, web_request


def test_ai_clients_initialization():
    llm = UniversalOpenAIClient(api_key="sk-test", base_url="https://api.openai.com/v1", model="gpt-4o")
    assert llm.api_key == "sk-test"
    assert llm.base_url == "https://api.openai.com/v1"
    assert llm.model == "gpt-4o"

    img = UniversalImageClient(api_key="sk-img", base_url="https://custom.ai/v1", model="dall-e-3")
    assert img.api_key == "sk-img"
    assert img.base_url == "https://custom.ai/v1"
    assert img.model == "dall-e-3"


@pytest.mark.asyncio
async def test_web_request_structure():
    # Проверяем структуру ответа без реального блокирующего сетевого вызова
    res = await web_request("https://localhost-test-fake.local", timeout=0.1)
    assert "status" in res
    assert "content" in res

