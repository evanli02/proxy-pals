import unittest
from unittest.mock import patch, MagicMock
from flask import json
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

from proxy_bot.proxy_controller import app

class TestProxyController(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    @patch("proxy_bot.proxy_controller.verify_slack_signature")
    def test_set_partners_invalid_signature(self, mock_verify):
        mock_verify.return_value = False
        response = self.app.post(
            "/proxy/slack/partners",
            data={"text": "user,partner,mimic"},
            headers={
                "X-Slack-Request-Timestamp": "1234567890",
                "X-Slack-Signature": "invalid",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json, {"error": "Invalid signature"})

    @patch("proxy_bot.proxy_controller.verify_slack_signature")
    @patch("proxy_bot.proxy_controller.update_partner_map_from_csv")
    def test_set_partners_no_clear_mode_success(self, mock_update, mock_verify):
        mock_verify.return_value = True
        mock_update.return_value = (1, [])
        
        response = self.app.post(
            "/proxy/slack/partners",
            data={"text": "user,partner,mimic"},
            headers={
                "X-Slack-Request-Timestamp": "1234567890",
                "X-Slack-Signature": "valid",
            },
        )
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["response_type"], "ephemeral")
        self.assertIn("Updated 1 partner mapping(s)", response.json["text"])
        mock_update.assert_called_once_with("user,partner,mimic")

    @patch("proxy_bot.proxy_controller.verify_slack_signature")
    @patch("proxy_bot.proxy_controller.update_partner_map_from_csv")
    @patch("proxy_bot.proxy_controller.process_clear_mode_if_requested")
    def test_set_partners_with_clear_mode_success(
        self, mock_process, mock_update, mock_verify
    ):
        mock_verify.return_value = True
        mock_update.return_value = (1, [])
        mock_process.return_value = "user,partner,mimic"
        
        response = self.app.post(
            "/proxy/slack/partners",
            data={"text": "-s,user,partner,mimic"},
            headers={
                "X-Slack-Request-Timestamp": "1234567890",
                "X-Slack-Signature": "valid",
            },
        )
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["response_type"], "ephemeral")
        self.assertIn("Updated 1 partner mapping(s)", response.json["text"])
        
        # Verify helper was called
        mock_process.assert_called_once_with("-s,user,partner,mimic")
        # Verify update was called with the returned text
        mock_update.assert_called_once_with("user,partner,mimic")

    @patch("proxy_bot.proxy_controller.verify_slack_signature")
    @patch("proxy_bot.proxy_controller.update_partner_map_from_csv")
    @patch("proxy_bot.proxy_controller.process_clear_mode_if_requested")
    def test_set_partners_clear_mode_failure(
        self, mock_process, mock_update, mock_verify
    ):
        mock_verify.return_value = True
        
        # Simulate archive failure
        mock_process.side_effect = Exception("DB Insert Error")
        
        response = self.app.post(
            "/proxy/slack/partners",
            data={"text": "-s,user,partner,mimic"},
            headers={
                "X-Slack-Request-Timestamp": "1234567890",
                "X-Slack-Signature": "valid",
            },
        )
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["response_type"], "ephemeral")
        self.assertEqual(response.json["text"], "Failed to update the data. Please try again.")
        
        # Update should NOT be called since archive failed
        mock_update.assert_not_called()

    @patch("proxy_bot.proxy_controller.verify_slack_signature")
    @patch("proxy_bot.proxy_controller.threading.Thread")
    def test_export_data_success(self, mock_thread, mock_verify):
        mock_verify.return_value = True
        
        response = self.app.post(
            "/proxy/slack/export",
            data={"text": "123456", "channel_id": "C123"},
            headers={
                "X-Slack-Request-Timestamp": "1234567890",
                "X-Slack-Signature": "valid",
            },
        )
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["response_type"], "ephemeral")
        self.assertEqual(response.json["text"], "Starting Excel export. The file will be uploaded shortly.")
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()

    @patch("proxy_bot.proxy_controller.verify_slack_signature")
    def test_export_data_invalid_password(self, mock_verify):
        mock_verify.return_value = True
        
        response = self.app.post(
            "/proxy/slack/export",
            data={"text": "wrong_password"},
            headers={
                "X-Slack-Request-Timestamp": "1234567890",
                "X-Slack-Signature": "valid",
            },
        )
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["response_type"], "ephemeral")
        self.assertEqual(response.json["text"], "Incorrect password or command.")

if __name__ == "__main__":
    unittest.main()
