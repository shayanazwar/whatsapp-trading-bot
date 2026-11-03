import pytest
from app.notifications.whatsapp import whatsapp_adapter

@pytest.mark.asyncio
async def test_whatsapp_is_enabled():
    assert whatsapp_adapter.is_enabled is True

@pytest.mark.asyncio
async def test_whatsapp_send_signal(mocker):
    # Mock httpx client post response for testing
    mock_response = mocker.AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"messages": [{"id": "wamid.test_signal"}]}
    
    mocker.patch("httpx.AsyncClient.post", return_value=mock_response)
    
    result = await whatsapp_adapter.send_signal("1234567890", "Test Kronos crypto signal")
    assert "messages" in result or result.get("status") == "simulated"
