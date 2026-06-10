from flask import Flask, jsonify, request
import logging
import threading

from validation_bot.validation_service import default_validation_service

app = Flask(__name__)
log = logging.getLogger("validation_controller")


def _run_validation_background(user_id: str):
    """Background task to run validation for a user and save it to the DB."""
    try:
        default_validation_service.run_and_save_validation(user_id)
        log.info(f"Successfully completed background validation for user {user_id}")
    except Exception as e:
        log.error(f"Error in background validation task for user {user_id}: {e}")


@app.route('/validation/<user_id>', methods=['GET', 'POST'])
def validation_endpoint(user_id):
    try:
        # Run the heavy validation process in a background thread
        thread = threading.Thread(target=_run_validation_background, args=(user_id,))
        thread.start()
        
        return jsonify({
            "status": "started", 
            "user_id": user_id, 
            "message": "Validation process started in the background."
        }), 200
        
    except Exception as e:
        log.error(f"Error starting validation endpoint for {user_id}: {e}")
        return jsonify({"error": str(e)}), 500

def _run_validation_with_masking_background(user_id: str, masking_questions: int):
    """Background task to run masked validation for a user and save it to the DB."""
    try:
        log.info(f"Starting background masked validation task for user: {user_id} with {masking_questions} masking questions.")
        default_validation_service.run_and_save_validation_with_masking(user_id, masking_questions)
        log.info(f"Successfully completed masked validation for user {user_id} with {masking_questions} masking questions.")
    except Exception as e:
        log.error(f"Error in background masked validation task for user {user_id}: {e}")

@app.route('/validation/masking/<user_id>', methods=['GET', 'POST'])
def validation_masking_endpoint(user_id):
    try:
        masking_questions = request.args.get('masking_questions', default=0, type=int)
        log.info(f"Received API request for masked validation: user_id={user_id}, masking_questions={masking_questions}")
        
        # Run the heavy masked validation process in a background thread
        thread = threading.Thread(target=_run_validation_with_masking_background, args=(user_id, masking_questions))
        thread.start()
        
        return jsonify({
            "status": "started", 
            "user_id": user_id, 
            "masking_questions": masking_questions,
            "message": f"Masked validation process started in the background with {masking_questions} questions masked."
        }), 200
        
    except Exception as e:
        log.error(f"Error starting masked validation endpoint for {user_id}: {e}")
        return jsonify({"error": str(e)}), 500
