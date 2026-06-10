import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Patch clients before loading app
patcher_slack = patch("commons.clients.make_slack_client")
mock_slack = patcher_slack.start()
mock_slack_client = MagicMock()
mock_slack.return_value = (mock_slack_client, "U12345678")

patcher_openai = patch("commons.clients.make_openai_client")
mock_openai = patcher_openai.start()
mock_openai_client = MagicMock()
mock_openai.return_value = mock_openai_client

os.environ["PROXY_SLACK_SIGNING_SECRET"] = "dummy"
os.environ["SLACK_SIGNING_SECRET"] = "dummy"
os.environ["OPENAI_API_KEY"] = "sk-dummy"

from learning_bot.learning_bot_service import _handle_message_internal, get_or_create_buffer, channel_buffers, _cornell_id_cache

class TestLearningBotService(unittest.TestCase):
    def setUp(self):
        # Clear buffers
        channel_buffers.clear()
        _cornell_id_cache.clear()
        
        # Reset mocks
        mock_slack_client.reset_mock()
        mock_openai_client.reset_mock()
        
    @patch("learning_bot.learning_bot_service.get_partner_maps_collection")
    @patch("learning_bot.learning_bot_service._user_has_cornell_id")
    @patch("learning_bot.learning_bot_service.web")
    @patch("learning_bot.learning_bot_service.oai")
    @patch("learning_bot.learning_bot_service.get_next_main_question")
    def test_first_message_sets_partner_map(self, mock_get_next, mock_oai, mock_web, mock_has_cornell_id, mock_get_partner_col):
        # Setup mocks
        mock_has_cornell_id.return_value = True
        mock_partner_col = MagicMock()
        mock_get_partner_col.return_value = mock_partner_col
        
        # Next question returns some question so it doesn't terminate immediately
        mock_get_next.return_value = {"id": "Q1", "main_question": "What is Q1?"}
        
        # Mock OAI response
        mock_response = MagicMock()
        mock_response.choices[0].message.content = '{"response": "Hello", "need_followup": false}'
        mock_oai.chat.completions.create.return_value = mock_response
        
        event = {
            "channel": "C123",
            "user": "U999",
            "text": "Hello there"
        }
        
        # Execute
        _handle_message_internal(event, "C123")
        
        # Verify buffer has 2 messages (system + user)
        buf = channel_buffers["C123"]
        self.assertEqual(len(buf.messages), 3) # system, user, assistant
        
        # Verify partner map was updated
        mock_get_partner_col.assert_called_once()
        mock_partner_col.update_one.assert_called_once()
        
        call_args = mock_partner_col.update_one.call_args[0]
        self.assertEqual(call_args[0], {"_id": "partner_map"})
        self.assertIn("map.U999", call_args[1]["$set"])

    @patch("learning_bot.learning_bot_service.get_partner_maps_collection")
    @patch("learning_bot.learning_bot_service._user_has_cornell_id")
    @patch("learning_bot.learning_bot_service.web")
    @patch("learning_bot.learning_bot_service.oai")
    @patch("learning_bot.learning_bot_service.get_next_main_question")
    @patch("learning_bot.learning_bot_service.farewell_message")
    def test_termination_when_no_next_question(self, mock_farewell, mock_get_next, mock_oai, mock_web, mock_has_cornell_id, mock_get_partner_col):
        mock_has_cornell_id.return_value = True
        mock_farewell.return_value = "Goodbye!"
        
        # Force next_question to be None
        mock_get_next.return_value = None
        
        event = {
            "channel": "C123",
            "user": "U999",
            "text": "My final answer"
        }
        
        # Make sure buffer has some history so it's not the first message
        buf = get_or_create_buffer("C123")
        buf.messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "x"}, {"role": "assistant", "content": "x"}]
        
        _handle_message_internal(event, "C123")
        
        # Verify farewell message was sent
        mock_web.chat_postMessage.assert_called_with(channel="C123", text="Goodbye!")
        
        # Verify OpenAI was NOT called because it returned early
        mock_oai.chat.completions.create.assert_not_called()

    @patch("learning_bot.learning_bot_service.get_partner_maps_collection")
    @patch("learning_bot.learning_bot_service._user_has_cornell_id")
    @patch("learning_bot.learning_bot_service.web")
    @patch("learning_bot.learning_bot_service.oai")
    @patch("learning_bot.learning_bot_service.get_next_main_question")
    def test_partner_map_initialization_retry_on_failure(self, mock_get_next, mock_oai, mock_web, mock_has_cornell_id, mock_get_partner_col):
        mock_has_cornell_id.return_value = True
        mock_partner_col = MagicMock()
        mock_get_partner_col.return_value = mock_partner_col
        
        # Simulate DB update failure on the first message
        mock_partner_col.update_one.side_effect = [Exception("DB down"), None]
        
        mock_get_next.return_value = {"id": "Q1", "main_question": "What is Q1?"}
        
        mock_response = MagicMock()
        mock_response.choices[0].message.content = '{"response": "Hello", "need_followup": false}'
        mock_oai.chat.completions.create.return_value = mock_response
        
        event1 = {"channel": "C123", "user": "U999", "text": "First message"}
        _handle_message_internal(event1, "C123")
        
        buf = channel_buffers["C123"]
        self.assertFalse(buf.partner_map_initialized) # Failed, so should be False
        self.assertEqual(mock_partner_col.update_one.call_count, 1)
        
        # Second message should trigger a retry
        event2 = {"channel": "C123", "user": "U999", "text": "Second message"}
        _handle_message_internal(event2, "C123")
        
        self.assertTrue(buf.partner_map_initialized) # Succeeded on retry
        self.assertEqual(mock_partner_col.update_one.call_count, 2)

if __name__ == "__main__":
    unittest.main()
