import unittest
from unittest.mock import patch, MagicMock
import numpy as np
import os
import sys

# Mock the environment variables needed by clients before importing
os.environ["OPENAI_API_KEY"] = "sk-dummy-key"
os.environ["MONGODB_URI"] = "mongodb://localhost:27017"

# We must mock make_slack_client BEFORE validation_service is imported, 
# because validation_service initializes the client at module level.
mock_make_slack = MagicMock(return_value=(MagicMock(), "U12345"))
sys.modules['commons.clients'] = MagicMock()
sys.modules['commons.clients'].make_slack_client = mock_make_slack
sys.modules['commons.clients'].make_openai_client = MagicMock()
sys.modules['commons.clients'].get_openai_model = MagicMock(return_value="gpt-5-mini")

# Adjust imports according to the structure of the module
from validation_bot.validation_service import ValidationService
from validation_bot.validation_repository import ValidationRepository
from validation_bot.models import ValidationPair, ValidationDocument, MessageTurn
from validation_bot.organize_messages_service import OrganizeMessagesService

class TestValidationService(unittest.TestCase):

    def setUp(self):
        self.mock_openai_client = MagicMock()
        self.mock_repo = MagicMock(spec=ValidationRepository)
        self.mock_organizer = MagicMock(spec=OrganizeMessagesService)
        self.service = ValidationService(self.mock_openai_client, self.mock_repo, self.mock_organizer)

    def test_calculate_similarities(self):
        # Setup mock embeddings
        user_answers = ["I love programming.", "Python is cool."]
        proxy_answers = ["I enjoy coding.", "Java is fast."]

        # Dummy vectors
        vec_u1 = [1.0, 0.0, 0.0]
        vec_u2 = [1.0, 0.0, 0.0]
        vec_p1 = [1.0, 0.0, 0.0]
        vec_p2 = [0.0, 1.0, 0.0]

        all_vecs = [vec_u1, vec_u2, vec_p1, vec_p2]

        # Construct the mock response
        mock_response = MagicMock()
        mock_response.data = []
        for i, vec in enumerate(all_vecs):
            mock_data_obj = MagicMock()
            mock_data_obj.embedding = vec
            mock_response.data.append(mock_data_obj)
        
        self.mock_openai_client.embeddings.create.return_value = mock_response

        # Execute
        similarities = self.service.calculate_similarities(user_answers, proxy_answers)

        # Assertions
        self.mock_openai_client.embeddings.create.assert_called_once_with(
            input=user_answers + proxy_answers,
            model="text-embedding-3-small"
        )
        
        self.assertEqual(len(similarities), 2)
        self.assertTrue(np.isclose(similarities[0], 1.0), f"Expected 1.0, got {similarities[0]}")
        self.assertTrue(np.isclose(similarities[1], 0.0), f"Expected 0.0, got {similarities[1]}")

    def test_extract_qa_pairs(self):
        messages = [
            {"role": "user", "content": "Hello bot"}, # Ignored (before first assistant)
            {"role": "assistant", "content": "How are you?"},
            {"role": "user", "content": "I am fine."}, # Pair 1
            {"role": "assistant", "content": "What is your favorite color?"},
            {"role": "assistant", "content": "Actually, what is your favorite animal?"}, # First Q ignored
            {"role": "user", "content": "I like dogs."},
            {"role": "user", "content": "And cats too."}, # Merged into Pair 2
            {"role": "assistant", "content": "Final question without answer"} # Ignored
        ]
        
        questions, answers = self.service.extract_qa_pairs(messages)
        
        self.assertEqual(len(questions), 2)
        self.assertEqual(len(answers), 2)
        
        self.assertEqual(questions[0], "How are you?")
        self.assertEqual(answers[0], "I am fine.")
        
        self.assertEqual(questions[1], "Actually, what is your favorite animal?")
        self.assertEqual(answers[1], "I like dogs. \n And cats too.")

    def test_calculate_similarities_empty(self):
        similarities = self.service.calculate_similarities([], [])
        self.assertEqual(similarities, [])
        self.mock_openai_client.embeddings.create.assert_not_called()

    @patch('validation_bot.validation_service.get_stateless_proxy_bot_answer')
    def test_run_validation_with_masking_integration(self, mock_proxy_answer):
        # Setup mock behavior
        self.mock_repo.get_messages_by_user.return_value = [
            {"role": "assistant", "content": "So, what's your favorite season?"},
            {"role": "user", "content": "I like summer."},
            {"role": "assistant", "content": "Are you a dog person?"},
            {"role": "user", "content": "Yes, I love dogs."}
        ]
        
        # Mock organizer output
        mock_turns = [
            MessageTurn(
                question="What is your favorite season?", 
                raw_question="So, what's your favorite season?",
                user_answer="I like summer.", 
                is_masked=False
            )
        ]
        self.mock_organizer.organize.return_value = mock_turns
        
        # Mock proxy answer 
        mock_proxy_answer.return_value = '{"response": "I prefer winter."}'
        
        # Mock similarities
        with patch.object(self.service, 'calculate_similarities', return_value=[0.85]):
            doc = self.service.run_validation_with_masking("user123", masking_questions=1)
            
            # Assertions
            self.assertEqual(doc.user_id, "user123")
            self.assertEqual(len(doc.masked_conversation), 1)
            self.assertEqual(doc.masked_conversation[0].question, "What is your favorite season?")
            self.assertEqual(doc.masked_conversation[0].user_answer, "I like summer.")
            self.assertEqual(doc.masked_conversation[0].proxy_answer, "I prefer winter.")
            self.assertEqual(doc.masked_conversation[0].similarity, 0.85)
            self.assertTrue(doc.masked_conversation[0].is_masked)
            self.assertEqual(doc.average_masked_similarity, 0.85)
            
            # Verify dependencies were called
            self.mock_repo.get_messages_by_user.assert_called_once_with("user123")
            self.mock_organizer.organize.assert_called_once()
            
            # Since masking_questions=1, the mock_turns[0] becomes masked
            # so its raw texts ("So, what's your favorite season?", "I like summer.") are dropped from custom_samples.
            # The remaining messages: "Are you a dog person?" and "Yes, I love dogs." will be in custom_samples.
            mock_proxy_answer.assert_called_once_with(
                text="What is your favorite season?", 
                requester_user_id="user123",
                custom_samples=["Question: Are you a dog person?", "user: Yes, I love dogs."]
            )

    @patch('validation_bot.validation_service.get_stateless_proxy_bot_answer')
    def test_run_validation_with_masking_preserves_chitchat(self, mock_proxy_answer):
        # The full conversation includes small talk, the masked question, and an unmasked question
        self.mock_repo.get_messages_by_user.return_value = [
            {"role": "user", "content": "Hi there!"},
            {"role": "assistant", "content": "Hello! So, what's your favorite season?"}, # Masked
            {"role": "user", "content": "I like summer."}, # Masked
            {"role": "assistant", "content": "Cool. And what is your favorite food?"}, # Not masked
            {"role": "user", "content": "I love pizza."}, # Not masked
            {"role": "assistant", "content": "That is nice."}, # Chitchat
            {"role": "user", "content": "Yeah."} # Chitchat
        ]
        
        # Organizer returns 2 turns
        mock_turns = [
            MessageTurn(
                question="What is your favorite season?", 
                raw_question="Hello! So, what's your favorite season?",
                user_answer="I like summer.", 
                is_masked=False # Will become True since masking_questions=1
            ),
            MessageTurn(
                question="What is your favorite food?", 
                raw_question="Cool. And what is your favorite food?",
                user_answer="I love pizza.", 
                is_masked=False
            )
        ]
        
        # Force random.sample to always pick the first turn to mask
        with patch('random.sample', return_value=[mock_turns[0]]):
            self.mock_organizer.organize.return_value = mock_turns
            mock_proxy_answer.return_value = '{"response": "dummy"}'
            
            with patch.object(self.service, 'calculate_similarities', return_value=[0.5, 0.9]):
                doc = self.service.run_validation_with_masking("user123", masking_questions=1)
                
                # Check is_masked
                self.assertTrue(doc.masked_conversation[0].is_masked)
                self.assertFalse(doc.masked_conversation[1].is_masked)
                
                # Check that custom_samples correctly excluded only the first turn's raw strings,
                # while keeping the greetings, the second turn, and the trailing chitchat.
                
                expected_samples = [
                    "user: Hi there!",
                    "Question: Cool. And what is your favorite food?",
                    "user: I love pizza.",
                    "Question: That is nice.",
                    "user: Yeah."
                ]
                
                # Assert that get_stateless_proxy_bot_answer was called with the correct custom_samples
                # for BOTH questions (the masked one and the unmasked one).
                self.assertEqual(mock_proxy_answer.call_count, 2)
                
                for call_args in mock_proxy_answer.call_args_list:
                    kwargs = call_args.kwargs
                    self.assertEqual(kwargs['custom_samples'], expected_samples)

if __name__ == '__main__':
    unittest.main()
