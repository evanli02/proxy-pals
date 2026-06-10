from textwrap import dedent

SCHEMA_INSTRUCTIONS = dedent("""
CRITICAL: You must respond with ONLY a valid JSON object. Do not include any markdown formatting, code blocks, or additional text.
Return ONLY a JSON object with these exact keys: 
summary, formality, cadence, punctuation, emoji_and_markers, lexicon, 
style_rules_do, style_rules_dont, safety_note.
- cadence: { avg_sentence_length: string, rhythm_notes: string }
- punctuation: { traits: string[] }
- emoji_and_markers: { emoji_frequency: string, markers: string[] }
- lexicon: { register: string, favorite_words: string[], hedges: string[], intensifiers: string[] }
IMPORTANT: Start your response with { and end with }. Do not wrap in ```json``` or any other formatting.
""").strip()
