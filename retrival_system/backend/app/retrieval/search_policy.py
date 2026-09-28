"""Candidate-only policy; evaluated by tests/test_retrieval_evaluation.py.

No threshold authorizes signing. Raw stage scores are not probabilities.
"""

POLICY_VERSION = "e5-trgm-review-v1"
FUZZY_MIN = 0.2
SEMANTIC_MIN = 0.83
AMBIGUITY_MARGIN = 0.03
