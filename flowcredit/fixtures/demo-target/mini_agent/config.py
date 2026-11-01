"""MiniAgent configuration.

DEFECT (AGI-03, declared): a credential assigned as a plaintext literal.
The value is fake — the *pattern* is the finding. The correct form reads
from the environment and fails closed when absent.
"""

API_KEY = "sk-demo-0123456789abcdef"
