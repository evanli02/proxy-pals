import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Add the project root to the sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Patch clients before loading app
patcher_slack = patch("commons.clients.make_slack_client")
mock_slack = patcher_slack.start()
mock_slack.return_value = (MagicMock(), "U12345678")

patcher_openai = patch("commons.clients.make_openai_client")
mock_openai = patcher_openai.start()
mock_openai.return_value = MagicMock()

os.environ["PROXY_SLACK_SIGNING_SECRET"] = "dummy"
os.environ["SLACK_SIGNING_SECRET"] = "dummy"
os.environ["OPENAI_API_KEY"] = "sk-dummy"

from proxy_bot import proxy_bot_service

class TestProxyBotService(unittest.TestCase):

    @patch("proxy_bot.proxy_bot_service.proxy_collection")
    @patch("proxy_bot.proxy_bot_service.load_partner_map_from_db")
    def test_load_proxy_conversation_doc_exists(self, mock_load_partner, mock_proxy_collection):
        """Test that if a proxy doc exists, we return it and DO NOT reload partner map."""
        mock_proxy_collection.find_one.return_value = {
            "channel_id": "C123",
            "style_rules": {"rule": 1},
            "sample_messages": ["user: hello"],
            "oa_messages": [],
            "conversation": []
        }
        
        result = proxy_bot_service.load_proxy_conversation("C123")
        
        self.assertIsNotNone(result)
        self.assertEqual(result["style_rules"], {"rule": 1})
        # Should not reload the partner map since we have existing state
        mock_load_partner.assert_not_called()

    @patch("proxy_bot.proxy_bot_service.proxy_collection")
    @patch("proxy_bot.proxy_bot_service.load_partner_map_from_db")
    def test_load_proxy_conversation_doc_missing(self, mock_load_partner, mock_proxy_collection):
        """Test that if a proxy doc does NOT exist, we reload partner map and return None."""
        mock_proxy_collection.find_one.return_value = None
        
        result = proxy_bot_service.load_proxy_conversation("C123")
        
        self.assertIsNone(result)
        # Should explicitly reload the partner map to guarantee perfect sync before initialization
        mock_load_partner.assert_called_once()
        
    @patch("proxy_bot.proxy_bot_service.proxy_collection", None)
    @patch("proxy_bot.proxy_bot_service.load_partner_map_from_db")
    def test_load_proxy_conversation_no_mongo_collection(self, mock_load_partner):
        """Test that if MongoDB proxy_collection is None, it returns None quickly."""
        result = proxy_bot_service.load_proxy_conversation("C123")
        
        self.assertIsNone(result)
        mock_load_partner.assert_not_called()

if __name__ == "__main__":
    unittest.main()
