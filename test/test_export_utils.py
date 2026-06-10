import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from transform_csv.export_utils import flatten_archived_conversations, flatten_partner_proxy_conversations, flatten_validation_data, export_conversations_to_excel_and_upload

class TestExportUtils(unittest.TestCase):
    @patch("transform_csv.export_utils.get_archived_proxy_collection")
    def test_flatten_archived_conversations(self, mock_get_collection):
        mock_collection = MagicMock()
        mock_get_collection.return_value = mock_collection
        
        mock_collection.find.return_value = [
            {
                "user_name": "Test User",
                "user_id": "U123",
                "oa_messages": [{"role": "system", "content": "You are a bot"}],
                "conversation": [
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "Hello"}
                ]
            }
        ]
        
        df = flatten_archived_conversations()
        
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["user_name"], "Test User")
        self.assertEqual(df.iloc[0]["user_id"], "U123")
        self.assertEqual(df.iloc[0]["oa_messages"], "[system]: You are a bot")
        self.assertEqual(df.iloc[0]["conversation"], "[user]: Hi\n\n[assistant]: Hello")

    @patch("transform_csv.export_utils.get_proxy_collection")
    def test_flatten_partner_proxy_conversations(self, mock_get_collection):
        mock_collection = MagicMock()
        mock_get_collection.return_value = mock_collection
        
        mock_collection.find.return_value = [
            {
                "user_name": "Partner User",
                "user_id": "U456",
                "conversation": [
                    {"role": "user", "content": "How are you?"},
                    {
                        "role": "assistant",
                        "content": "I am fine.",
                        "metadata": {
                            "category": "greeting",
                            "action": "reply",
                            "has_prior_knowledge": "yes",
                            "confidence": "high",
                            "extracted_question": "how are you",
                            "user_query": "How are you?"
                        }
                    }
                ]
            }
        ]
        
        df = flatten_partner_proxy_conversations()
        
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["user_name"], "Partner User")
        self.assertEqual(df.iloc[0]["user_id"], "U456")
        self.assertEqual(
            df.iloc[0]["conversation"],
            "[user]: How are you?\n\n[assistant] (Meta: category: greeting, action: reply, has_prior_knowledge: yes, confidence: high, extracted_question: how are you, user_query: How are you?): I am fine."
        )

    @patch("transform_csv.export_utils.flatten_validation_data")
    @patch("transform_csv.export_utils.flatten_partner_proxy_conversations")
    @patch("transform_csv.export_utils.flatten_archived_conversations")
    @patch("transform_csv.export_utils.flatten_conversations")
    def test_export_conversations_excel_upload(self, mock_flatten, mock_flatten_archived, mock_flatten_partner, mock_flatten_validation):
        mock_df = pd.DataFrame([{"test": "data"}])
        mock_flatten.return_value = mock_df
        mock_flatten_archived.return_value = mock_df
        mock_flatten_partner.return_value = mock_df
        
        mock_validation_df = pd.DataFrame([
            {
                "user_id": "U123",
                "average_masked_similarity": 0.5,
                "average_similarity": 0.6,
                "similarity": 0.7,
                "test": "data1"
            },
            {
                "user_id": "U123",  # Duplicate user
                "average_masked_similarity": 0.5,
                "average_similarity": 0.6,
                "similarity": 0.8,
                "test": "data2"
            },
            {
                "user_id": "U456",  # Different user
                "average_masked_similarity": 0.9,
                "average_similarity": 0.9,
                "similarity": 0.9,
                "test": "data3"
            }
        ])
        mock_flatten_validation.return_value = mock_validation_df
        
        mock_web_client = MagicMock()
        mock_web_client.files_upload_v2.return_value = {"file": {"id": "F123"}}
        
        export_conversations_to_excel_and_upload(mock_web_client, "C123")
        
        self.assertEqual(mock_web_client.files_upload_v2.call_count, 1)
        call_args = mock_web_client.files_upload_v2.call_args[1]
        
        # Check upload parameters
        self.assertEqual(call_args["channel"], "C123")
        self.assertIn("exported_data_", call_args["filename"])
        self.assertTrue(call_args["filename"].endswith(".xlsx"))
        
        # Check that data was properly formatted (duplicates cleared)
        # First row for U123 should be kept intact
        self.assertEqual(mock_validation_df.iloc[0]["user_id"], "U123")
        self.assertEqual(mock_validation_df.iloc[0]["average_masked_similarity"], 0.5)
        
        # Second row for U123 should have grouping columns cleared
        self.assertEqual(mock_validation_df.iloc[1]["user_id"], "")
        self.assertEqual(mock_validation_df.iloc[1]["average_masked_similarity"], "")
        self.assertEqual(mock_validation_df.iloc[1]["average_similarity"], "")
        self.assertEqual(mock_validation_df.iloc[1]["similarity"], 0.8) # other columns kept intact
        
        # First row for U456 should be kept intact
        self.assertEqual(mock_validation_df.iloc[2]["user_id"], "U456")
        self.assertEqual(mock_validation_df.iloc[2]["average_masked_similarity"], 0.9)

    @patch("transform_csv.export_utils.get_validation_collection")
    def test_flatten_validation_data(self, mock_get_collection):
        mock_collection = MagicMock()
        mock_get_collection.return_value = mock_collection
        
        mock_collection.find.return_value = [
            {
                "user_id": "U123",
                "average_masked_similarity": 0.5,
                "average_similarity": 0.6,
                "masked_conversations": [
                    {
                        "question": "Q1",
                        "raw_question": "R1",
                        "user_answer": "UA1",
                        "proxy_answer": "PA1",
                        "is_masked": True,
                        "similarity": 0.4
                    }
                ],
                "conversation": [
                    {
                        "question": "Q2",
                        "raw_question": "R2",
                        "user_answer": "UA2",
                        "proxy_answer": "PA2",
                        "is_masked": False,
                        "similarity": 0.7
                    }
                ]
            }
        ]
        
        df = flatten_validation_data()
        
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 2)  # One for masked, one for unmasked
        
        # Check first row (masked)
        self.assertEqual(df.iloc[0]["user_id"], "U123")
        self.assertEqual(df.iloc[0]["conversation_type"], "masked")
        self.assertEqual(df.iloc[0]["question"], "Q1")
        self.assertEqual(df.iloc[0]["average_masked_similarity"], 0.5)
        
        # Check second row (unmasked)
        self.assertEqual(df.iloc[1]["user_id"], "U123")
        self.assertEqual(df.iloc[1]["conversation_type"], "unmasked")
        self.assertEqual(df.iloc[1]["question"], "Q2")
        self.assertEqual(df.iloc[1]["average_similarity"], 0.6)

if __name__ == "__main__":
    unittest.main()
