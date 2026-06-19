# -*- coding: utf-8 -*-
"""Shared mutable runtime state (replaces reassigned globals)."""
import os


class _RuntimeState:
    nsgifts_client = None
    chatgpt_webapp_enabled = os.getenv("CHATGPT_WEBAPP_ENABLED", "1") == "1"
    claude_webapp_enabled = os.getenv("CLAUDE_WEBAPP_ENABLED", "1") == "1"
    perplexity_webapp_enabled = os.getenv("PERPLEXITY_WEBAPP_ENABLED", "1") == "1"


rt = _RuntimeState()
